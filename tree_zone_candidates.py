"""
tree_zone_candidates.py

Tree/windbreak candidate zone identification -- Scale of Permanence step 5
(Trees), which precedes Permanent Buildings (step 6, solar_suitability.py)
and follows Water (step 4, water_candidate_zones.py) and, indirectly,
Land Shape/Access (steps 2-3, production_area.py/road_corridors.py). Trees
is currently the only zone-type layer in this pipeline with no structured
geometry -- it's narrative-only in report generation today, the same gap
production and water candidate zones used to have before production_area.py/
production_suitability.py and water_candidate_zones.py replaced their own
prose-only step with real computed geometry. This module closes that same
gap for Trees, reusing already-built infrastructure rather than inventing a
new data source or scoring philosophy.

Because Trees (step 5) precedes Permanent Buildings (step 6) in Scale of
Permanence ordering, this module has NO awareness of solar/structure
candidates at all -- it's solar siting that should eventually account for
tree zones (a later, separate pass), not the reverse.

    STEP 1 -- SEARCH SPACE (compute_tree_search_space()):
        the full property boundary, minus real geometry (via .difference(),
        not a heuristic mask) for:
          - EVERY OPTIMIZED production-zone candidate
            (production_area_ceiling.identify_optimized_production_areas()'s
            scored/soil-carved/ceiling-trimmed output -- ALL of them; no
            zone-SELECTION mechanism exists for production, so "every
            currently-optimized candidate" is the correct input here).
            This is the ceiling-trimmed result, NOT
            production_area.identify_production_areas()'s full, un-trimmed
            candidates -- production_area.py's slope+hydric eligibility
            gate on its own claims a very large fraction of a real
            reference property as eligible ground, which production_area_ceiling.py
            already exists specifically to trim toward a documented,
            more-plausible ceiling (PRODUCTION_CEILING_PCT_OF_PARCEL,
            80% of parcel by default) before anything downstream treats it
            as "claimed." Confirmed live: search space barely changed
            (0.72 -> 0.56 acres) when only water/road selection was
            rewired, because the un-trimmed ~95%-of-parcel patch already
            consumed almost everything selection could have freed up --
            swapping in the optimized output is what actually recovers
            the withheld acreage.
          - the SINGLE selected water-system zone
            (water_suitability.select_optimal_water_zone()) -- not every
            water candidate. Per product decision, this app targets small
            farms only: one well-suited water zone is sufficient, so only
            that one zone's own geometry counts as "claimed."
          - the SINGLE selected road corridor
            (road_corridors.select_optimal_road_corridor()) -- not every
            road candidate, same "one well-suited candidate is enough for
            a small farm" reasoning. Still subtracted as real, zero-width
            LineString geometry, not buffered -- unchanged in spirit from
            before: a zero-width line has a practically negligible effect
            on the resulting search-space AREA, which is expected, not a
            bug (an eventual pass modeling actual road construction/
            right-of-way would be the place to introduce a real cleared
            buffer, not this one).
        The remainder is the search space Step 2 scores.

        GEOMETRY FORM CLAIMED: each of the three inputs above is
        subtracted using that zone's own OPTIMIZED/render form, NOT its
        raw cell-footprint geometry -- the same shapes render_layout_map.py
        itself draws, per the reasoning already established there
        (production_area.py's/water_candidate_zones.py's own module
        docstrings for the two hulls, this module's own ROAD CORRIDOR
        STYLE section for the smoothed line):
          - production: render_fill_polygon_utm (production_area.py's
            plain convex hull of each patch's own render_polygon_utm,
            re-intersected with the boundary) -- NOT polygon_utm, the
            blocky, notched raw cell-union footprint. A real interior
            pocket (excluded steep/hydric cells) or waist-split pinch in
            polygon_utm would otherwise read as "still open" ground here,
            when the property's actual final layout (what render_layout_
            map.py draws, what a reader actually sees) shows it as part of
            one coherent production zone.
          - water: same reasoning, same field --
            water_candidate_zones.find_candidate_zones()'s own
            render_fill_polygon_utm for the single selected zone, not its
            polygon_utm.
          - road: render_layout_map.py's own _smooth_line_for_render()
            (Douglas-Peucker simplify, then Chaikin corner-cutting) run
            directly on the selected route's real line_utm -- the same
            display transform render_layout_map.py applies before
            plotting, applied here in the DEM's own real UTM meters
            (line_utm's native CRS) rather than the approximate,
            latitude-distorted meters render_layout_map.py's own Web
            Mercator rendering pass uses. road_corridors.py's raw
            line_utm is a literal per-DEM-cell stairstepped polyline (see
            that module's own module docstring) -- smoothing it here
            before subtraction keeps this module's claimed road geometry
            visually consistent with the corridor actually shown on the
            final map, not an artificially blockier one. Still a
            zero-width LineString either way, so this remains a
            practically negligible effect on the resulting search-space
            AREA (see above) -- this only changes the line's SHAPE, not
            whether it's buffered.
        In every case this is the SAME "reads as one coherent shape/
        alignment" reasoning already established for rendering -- applied
        here to what tree zones treat as already-claimed, not just to
        what's drawn on the map. None of this touches any zone's own real
        polygon_utm/geometry_wgs84/line_utm (used for that zone's own
        scoring, eligibility, and narrative report) -- only the copy of
        the geometry handed to compute_tree_search_space() here.

    STEP 2 -- CANOPY EXCLUSION GATE, THEN SUITABILITY SCORING
        (score_tree_search_space()):
        before any of the four factors below are even computed, a cell
        already inside real, EXISTING tree canopy (production_area.
        get_required_tree_root_zone_mask_utm() -- the SAME mandatory,
        non-degrading "fetch canopy, or fail hard" building block
        production_area.py/production_area_ceiling.py/water_candidate_
        zones.py all already use for their own canopy gates, at this
        module's own TREE_ZONE_CANOPY_BUFFER_METERS, which reuses
        production's own TREE_ROOT_ZONE_BUFFER_METERS, 10ft, unchanged)
        is HARD-EXCLUDED from candidacy entirely -- it never enters the
        composite score at all, the same "excluded before scoring, not
        merely scored low" treatment as being outside the search space in
        the first place. This module identifies NEW tree-suitable ground;
        ground that's already wooded doesn't need a "candidate"
        designation. A canopy fetch failure is NOT gracefully degraded
        here (unlike the three Step 2 factors below) -- same "can't
        verify this is free of/covered by tree cover, refuse rather than
        guess" hard-failure reasoning production_area.py's own mandatory
        canopy gate already uses.

        Every DEM cell that survives this gate is then scored against
        four independently-stored 0-1 factors, reusing data this pipeline
        already fetches elsewhere -- no new data sources:
          - soil_marginality_factor: real SSURGO Farmland Classification
            (soil_data.get_farmland_classification_for_polygon() +
            is_prime_farmland() -- the same field/logic solar_suitability.py
            already uses to FLAG, not score, a prime-farmland conflict),
            INVERTED here: prime/conditionally-prime farmland scores LOW
            (it's genuinely good agricultural ground -- the opposite of
            marginal); anything else scores HIGH. See its own weight
            constant for why this is necessarily binary (0.0/1.0), not a
            finer gradient.
          - slope_factor: real DEM slope (production_area.py's own
            compute_slope_percent(), UNCHANGED), INVERTED from
            production's scoring -- steeper scores HIGHER here, since
            erosion-prone, poor-cultivation ground is well-suited to tree
            cover. See TREE_SLOPE_REFERENCE_PCT for how this scales across
            the full slope range this search space can contain (up to the
            property's own known 25-80% Gilpin-Weikert-Culleoka complex --
            far beyond production_area.py's own 20% ceiling, which never
            had to score anything that steep).
          - hydric_overlap_factor: real SSURGO hydric-soil geometry -- the
            SAME shared mukey-selection logic production_suitability.py's
            own soil-carving already uses
            (soil_data.hydric_disqualifying_mukeys(), same
            MIN_HYDRIC_COMPONENT_PCT_TO_EXCLUDE threshold), just fetched
            once for the whole boundary rather than per production-patch.
            Genuinely wet/hydric ground is well-suited to riparian tree
            cover -- weighted as the STRONGEST positive factor here (see
            HYDRIC_OVERLAP_FACTOR_WEIGHT), not a mild bonus.
          - stream_proximity_factor: a MINOR, distance-based bonus near
            real NHD-mapped streams (hydrology_data.get_water_features_for_
            boundary()'s own 'streams' key -- not water bodies), scored
            INDEPENDENTLY of hydric_overlap_factor above -- a location can
            be near a stream without itself sitting on hydric soil, or
            vice versa (a hydric inclusion away from any mapped stream).
        Cells are grouped into connected candidate patches
        (raster_grid.connected_components(), same pattern as
        production_area.py/road_corridors.py) wherever their composite
        tree_suitability_score clears MIN_TREE_SUITABILITY_SCORE.

    STEP 3 -- THRESHOLD + SIZE FILTER (also score_tree_search_space()):
        a patch below MIN_TREE_SUITABILITY_SCORE is dropped even if it's
        real, unclaimed leftover land -- being merely unclaimed is NOT
        enough to qualify as tree-suitable by itself (see that constant's
        own reasoning). A patch that DOES clear the threshold but is too
        small/fragmented (below MIN_TREE_ZONE_ACRES) is also dropped -- same
        "exclude slivers" role as production_area.py's own
        MIN_PRODUCTION_AREA_ACRES.

This module does NOT assign a specific function to a resulting zone
(windbreak vs. riparian buffer vs. habitat corridor vs. anything else) --
that's left entirely to report narrative, a separate later pass (see
confidence_notes on the output feature, which says so explicitly). It also
does NOT wire into generate_full_report.py/report_generator.py's prompt in
this pass -- same "validate the layer on its own first" framing every other
zone-type layer in this pipeline used before being narrated.

score_tree_search_space() is the pure scoring core -- takes an already-
computed search-space polygon plus already-fetched soil/stream geometry
unions, does no network I/O itself, same reasoning as every other
*_suitability.py/*_candidate_zones.py module in this pipeline (independently
testable against a synthetic DEM). identify_tree_zone_candidates() is the
full fetch-and-score entry point.
"""

import math
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import Point, Polygon, box, mapping, shape
from shapely.ops import unary_union
from shapely.prepared import prep

from canopy_height_data import TREE_ROOT_ZONE_BUFFER_METERS
from dem_data import get_dem_for_boundary
from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from hydrology_data import get_water_features_for_boundary
from production_area import MIN_PRODUCTION_AREA_ACRES, compute_slope_percent, get_required_tree_root_zone_mask_utm
from production_area_ceiling import identify_optimized_production_areas
from raster_grid import SQUARE_METERS_PER_ACRE, connected_components, pixel_center_xy
from road_corridors import identify_road_corridor_candidates
from soil_data import (
    coordinates_to_wkt_polygon,
    get_farmland_classification_for_polygon,
    get_soil_data_for_polygon,
    get_soil_geometries_for_polygon,
    hydric_disqualifying_mukeys,
    is_prime_farmland,
)
from water_suitability import identify_water_suitability

# --- composite weights (must sum to 1.0). CONFIGURABLE -- tune against a
# real property once ground-truthed, same "documented but adjustable"
# standard as every other weighted-factor module in this pipeline.
#
# hydric_overlap gets the LARGEST weight -- per this feature's own spec,
# genuinely wet/hydric ground is a stronger, more direct signal for
# riparian tree suitability than mere proximity to a mapped stream, so it's
# weighted meaningfully higher, not just nudged above the others.
# slope is second -- a real, well-established erosion/cultivation-difficulty
# signal, inverted from production's own scoring.
# soil_marginality is third -- a real but coarse (binary, see its own
# comment) agricultural-quality signal.
# stream_proximity is deliberately the SMALLEST weight -- per this
# feature's own spec, "a minor positive factor," independent of and weaker
# than the direct hydric-overlap signal above.
HYDRIC_OVERLAP_FACTOR_WEIGHT = 0.40
SLOPE_FACTOR_WEIGHT = 0.30
SOIL_MARGINALITY_FACTOR_WEIGHT = 0.20
STREAM_PROXIMITY_FACTOR_WEIGHT = 0.10

_WEIGHT_SUM = (
    HYDRIC_OVERLAP_FACTOR_WEIGHT + SLOPE_FACTOR_WEIGHT + SOIL_MARGINALITY_FACTOR_WEIGHT + STREAM_PROXIMITY_FACTOR_WEIGHT
)
assert math.isclose(_WEIGHT_SUM, 1.0, abs_tol=1e-6), f"tree suitability factor weights must sum to 1.0, got {_WEIGHT_SUM}"

# tree_suitability_score is reported on a 0-100 scale (composite of the 0-1
# factors above, rounded to 1 decimal) -- same convention
# production_suitability.py/solar_suitability.py already use for their own
# suitability_score, so a reader comparing layers doesn't have to remember
# a different scale per layer. The four individual *_factor properties stay
# on their native 0-1 scale.
SUITABILITY_SCORE_SCALE = 100

# Slope grade (percent) at/above which additional steepness stops adding
# further tree-suitability credit -- ground already reads as unambiguously
# marginal-for-cultivation, erosion-prone terrain well before this point.
# This property's own SSURGO data already identifies a 25-80% Gilpin-
# Weikert-Culleoka complex as a distinct, extreme category -- production_
# area.py's own MAX_PRODUCTION_SLOPE_PCT (20%) never had to differentiate
# anywhere in that range at all. 50% is chosen deliberately INSIDE that
# known 25-80% range (not at its low end, which would barely extend past
# production's old 20% ceiling, and not at its extreme high end, which
# would compress all the practically-relevant differentiation into a
# narrow band near the top): a linear ramp from 0% to 50% grade keeps
# discriminating meaningfully across the entire range production never had
# to consider, while ground beyond 50% (up to the property's known 80%
# extreme) is treated as uniformly, maximally tree-suited -- a defensible
# domain judgment, since there's no reason 80% grade is MORE tree-suited
# than 55% grade once cultivation is already out of the question; the
# differentiation that actually matters is the mild-to-steep transition
# this range covers, not further distinguishing among already-extreme
# slopes. CONFIGURABLE.
TREE_SLOPE_REFERENCE_PCT = 50.0

# How far a location can sit from a mapped NHD stream and still earn a
# partial stream_proximity_factor bonus, falling linearly to 0.0 at this
# distance (1.0 right at a stream). No citable "optimal riparian buffer
# distance for windbreak/shelterbelt siting" figure exists to pin this to --
# stated plainly, same standard as this codebase's own MAX_ROAD_GRADE_PCT
# reasoning (road_corridors.py). Set to the same order-of-magnitude
# reference distance solar_suitability.py already uses for its own
# edge-proximity scoring (PRODUCTION_PROXIMITY_REFERENCE_METERS, 100m) -- a
# generic "close enough to plausibly matter, not literally touching"
# distance already established elsewhere in this pipeline, rather than
# inventing an unrelated new number. CONFIGURABLE.
STREAM_PROXIMITY_REFERENCE_METERS = 100.0

# Below this composite score (0-100 scale), leftover/unclaimed land is
# treated as merely UNCOMPETED-FOR, not genuinely tree-suitable -- being
# unclaimed by production/water/roads is not, by itself, evidence that
# ground is marginal. Ground that's flat (slope_factor near 0), not hydric
# (hydric_overlap_factor 0.0), and simply not prime farmland (a common,
# fairly weak condition -- soil_marginality_factor 1.0, contributing only
# SOIL_MARGINALITY_FACTOR_WEIGHT*100 = 20 points) with no stream nearby
# (stream_proximity_factor 0.0) composites to ~20/100 -- well below this
# threshold, so "just not prime farmland" alone never qualifies. Lowered
# from an earlier, stricter 50.0 (the 0-100 midpoint) to 31.0 -- a real
# product call to admit more marginal, merely-not-prime ground as a tree
# candidate than the original midpoint allowed. 31.0 (not some lower
# number, e.g. 25.0) is deliberately chosen to sit one point ABOVE the
# highest score soil_marginality_factor + stream_proximity_factor can
# EVER reach together with no hydric or slope signal at all (a non-prime
# patch sitting directly on a mapped stream: SOIL_MARGINALITY_FACTOR_
# WEIGHT*100 + STREAM_PROXIMITY_FACTOR_WEIGHT*100 = 20 + 10 = 30) -- so a
# real, positive hydric_overlap_factor or slope_factor signal is still
# REQUIRED for any candidate to qualify, exactly as at 50.0; stream
# proximity alone still can never clear this threshold on its own, even
# at maximum proximity. CONFIGURABLE -- same "documented, not just
# asserted" standard as MIN_PRODUCTION_AREA_ACRES/solar_suitability.py's
# own MIN_SUITABILITY_SCORE. If this ever needs to move again, keep this
# invariant in mind: it must stay strictly above SOIL_MARGINALITY_FACTOR_
# WEIGHT*100 + STREAM_PROXIMITY_FACTOR_WEIGHT*100 for that invariant to
# keep holding.
MIN_TREE_SUITABILITY_SCORE = 31.0

# Minimum contiguous size for a scored patch to be reported as a real tree
# zone candidate, not a fragmented sliver -- reuses production_area.py's
# own MIN_PRODUCTION_AREA_ACRES directly rather than an independently-tuned
# value: there's no evidence a tree zone needs a different usable-size floor
# than a production zone does, and reusing the same constant means the two
# floors can never silently drift apart. CONFIGURABLE (override the kwarg
# directly if a real property ever needs a different floor for this layer
# specifically).
MIN_TREE_ZONE_ACRES = MIN_PRODUCTION_AREA_ACRES

# Buffer (meters) around EXISTING tree canopy (real USGS 3DEP lidar HAG
# coverage, canopy_height_data.tree_root_zone_mask()) within which a DEM
# cell is HARD-EXCLUDED from tree-zone candidacy entirely -- a cell
# already under (or within this buffer of) real, existing tree cover
# doesn't need a "candidate" designation; this layer identifies NEW
# tree-suitable ground, not ground that's already wooded. This is a
# GATE, not one of the four scored factors above: an excluded cell never
# enters composite_grid at all, same "excluded before scoring, not
# merely scored low" treatment production_area.py's own tree_root_zone_
# hit gate gives production zones (see that module's own
# get_required_tree_root_zone_mask_utm()). Reuses production_area.py's
# own TREE_ROOT_ZONE_BUFFER_METERS value directly (10ft) rather than an
# independently-tuned buffer -- there's no evidence this layer needs a
# different existing-canopy clearance than production zones do, and
# reusing the same constant means the two can never silently drift apart
# (same reasoning as MIN_TREE_ZONE_ACRES reusing MIN_PRODUCTION_AREA_ACRES
# directly, just above). CONFIGURABLE (override get_required_tree_root_
# zone_mask_utm's own buffer_meters kwarg directly if a real property
# ever needs a different clearance for this layer specifically).
TREE_ZONE_CANOPY_BUFFER_METERS = TREE_ROOT_ZONE_BUFFER_METERS

# Neutral factor value used when a given factor's own data source couldn't
# be reached at all (fetch failure) -- same "missing/inapplicable data
# should neither reward nor penalize" convention solar_suitability.py's own
# _production_proximity_score() already established for its analogous
# None-distance case ("there's no ... geometry to be near or far from, so
# this axis shouldn't reward or penalize the candidate either way").
# Applied uniformly across all three network-fetched factors here, rather
# than inventing a different missing-data convention per factor.
_NEUTRAL_FACTOR_VALUE = 0.5

TREE_ZONE_CONFIDENCE_NOTES_TEMPLATE = (
    "This identifies GENERAL tree-suitable land -- ground within the property's leftover, "
    "non-claimed area (the full boundary minus every current OPTIMIZED (ceiling-trimmed) "
    "production-zone candidate plus the single SELECTED water-system zone and single SELECTED "
    "road corridor's own geometry, MINUS real, existing tree canopy (production_area.py's own "
    "TREE_ROOT_ZONE_BUFFER_METERS buffer around real USGS 3DEP lidar canopy coverage -- this "
    "layer identifies NEW tree-suitable ground, not ground that's already wooded) that scores "
    "above a minimum suitability threshold on marginality relative to production use. It is NOT "
    "a windbreak, riparian buffer, habitat "
    "corridor, or any other specific planting plan -- assigning that kind of function/purpose to "
    "this ground is deliberately left to report narrative, a separate later pass, and is NOT "
    "decided here. It is also NOT a species recommendation. tree_suitability_score (0-100) is a "
    "weighted composite of FOUR independently-stored 0-1 factors: hydric_overlap_factor (weight "
    "{hydric_weight}, the strongest -- real SSURGO hydric-soil geometry, the same shared "
    "mukey-selection logic production_suitability.py's own soil-carving already uses), "
    "slope_factor (weight {slope_weight}, real DEM slope, INVERTED from production scoring -- "
    "steeper scores higher here, since erosion-prone, poor-cultivation ground is well-suited to "
    "tree cover), soil_marginality_factor (weight {soil_weight}, real SSURGO Farmland "
    "Classification, INVERTED from how solar_suitability.py uses the same field -- prime/"
    "conditionally-prime farmland scores LOW here, everything else scores HIGH; necessarily a "
    "binary 0.0/1.0 factor, not a finer agricultural-capability gradient, since no finer SSURGO "
    "field is fetched anywhere in this pipeline), and stream_proximity_factor (weight "
    "{stream_weight}, the smallest -- a distance-based bonus near real NHD-mapped streams, scored "
    "INDEPENDENTLY of hydric_overlap_factor: a location can be near a stream without itself "
    "sitting on hydric soil, or vice versa). {data_availability_note}Leftover land that's merely "
    "unclaimed but doesn't clear the minimum suitability threshold is deliberately NOT included as "
    "a candidate here -- see this module's MIN_TREE_SUITABILITY_SCORE for that reasoning. This is "
    "a topographic + soil + hydrology candidate zone, not a certainty -- ground-truth before "
    "committing to it. It also inherits the limitations of every layer it's built on "
    "(production_suitability.py, water_candidate_zones.py, road_corridors.py, and the SSURGO/NHD/"
    "DEM sources those already depend on)."
)

TREE_SEARCH_SPACE_CONFIDENCE_NOTES = (
    "Diagnostic layer only, not a deliverable candidate zone: the full property boundary minus "
    "every current OPTIMIZED (ceiling-trimmed) production-zone candidate plus the single SELECTED "
    "water-system zone and single SELECTED road corridor's own geometry (see "
    "tree_zone_candidates.py's module docstring, Step 1). Useful for checking the search space "
    "independently of the suitability scoring/thresholding built on top of it (Steps 2-3)."
)


def _slope_factor(slope_pct: float, reference_pct: float = TREE_SLOPE_REFERENCE_PCT) -> float:
    """INVERTED from production_area.py's slope scoring: steeper slope
    scores HIGHER for tree suitability. Linear from 0% grade (0.0) to
    reference_pct grade (1.0), holding at 1.0 beyond -- see
    TREE_SLOPE_REFERENCE_PCT's own comment for why 50% and not some other
    saturation point."""
    if slope_pct <= 0:
        return 0.0
    return max(0.0, min(1.0, slope_pct / reference_pct))


def _stream_proximity_factor(
    distance_m: Optional[float], reference_meters: float = STREAM_PROXIMITY_REFERENCE_METERS
) -> float:
    """0-1 distance-based bonus: 1.0 right at a mapped stream, falling
    linearly to 0.0 at reference_meters or beyond. distance_m is None only
    when stream data was genuinely, successfully fetched and found nothing
    nearby at all (a real "no stream" answer) -- that also scores 0.0, the
    same as "too far away to matter." A missing-DATA (fetch failed) case is
    handled by the caller via _NEUTRAL_FACTOR_VALUE, not here."""
    if distance_m is None:
        return 0.0
    return max(0.0, min(1.0, 1.0 - distance_m / reference_meters))


def _polygonal_parts(geom):
    """A .difference()/.intersection() between real-world polygons can, in
    edge cases (touching only along a shared edge or at a single vertex),
    return a GeometryCollection mixing stray points/lines in with the real
    remaining area -- same reasoning as soil_data.py's own _polygonal_parts().
    Only Polygon/MultiPolygon parts represent real ground; this drops
    everything else."""
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    if geom.geom_type == "GeometryCollection":
        polys = [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        return unary_union(polys) if polys else None
    return None


def compute_tree_search_space(
    boundary_polygon_utm: Polygon,
    production_polygons_utm: list,
    water_polygons_utm: list,
    road_lines_utm: list,
) -> tuple[object, Optional[object]]:
    """
    Step 1 -- pure geometry difference (see module docstring): the real
    parcel boundary, minus the union of every currently-claimed production/
    water/road candidate geometry. Road geometry is subtracted as real,
    zero-width LineStrings, NOT buffered -- see module docstring for why
    that's expected to have a negligible practical effect on the resulting
    area this pass.

    Returns (search_space, claimed_union):
      - search_space is None if the claimed union completely covers the
        boundary (leftover land is None, not merely empty, distinguishing
        "nothing to search" as a real, reportable outcome); a Polygon/
        MultiPolygon otherwise (possibly boundary_polygon_utm itself,
        unmodified, if nothing was claimed at all).
      - claimed_union is None if production_polygons_utm, water_polygons_utm,
        and road_lines_utm are ALL empty (nothing on this property has been
        claimed by any upstream layer yet) -- distinct from a real, non-None
        union that happens to leave a search_space of remaining area.
    """
    pieces = []
    if production_polygons_utm:
        pieces.append(unary_union(production_polygons_utm))
    if water_polygons_utm:
        pieces.append(unary_union(water_polygons_utm))
    if road_lines_utm:
        pieces.append(unary_union(road_lines_utm))

    claimed_union = unary_union(pieces) if pieces else None

    if claimed_union is None:
        return boundary_polygon_utm, None

    search_space = _polygonal_parts(boundary_polygon_utm.difference(claimed_union))
    return search_space, claimed_union


def score_tree_search_space(
    dem: dict,
    search_space_utm,
    boundary_polygon_utm: Polygon,
    prime_farmland_union: Optional[object] = None,
    prime_farmland_data_available: bool = True,
    hydric_union: Optional[object] = None,
    hydric_data_available: bool = True,
    stream_union: Optional[object] = None,
    stream_data_available: bool = True,
    tree_root_zone_mask_utm: Optional[np.ndarray] = None,
    slope_reference_pct: float = TREE_SLOPE_REFERENCE_PCT,
    stream_proximity_reference_meters: float = STREAM_PROXIMITY_REFERENCE_METERS,
    min_score: float = MIN_TREE_SUITABILITY_SCORE,
    min_area_acres: float = MIN_TREE_ZONE_ACRES,
) -> list[dict]:
    """
    Pure scoring core -- Steps 2-3 (see module docstring). Takes an
    already-computed search-space polygon (Step 1's own output) plus
    already-fetched soil/stream geometry unions; does no network I/O or
    upstream zone computation itself.

    *_union is a shapely geometry already in dem['crs'], or None (either
    "checked, genuinely nothing found" when its own *_data_available flag
    is True, or "couldn't check at all" when that flag is False -- see
    _NEUTRAL_FACTOR_VALUE for how the latter is scored).

    tree_root_zone_mask_utm is a pre-fetched boolean np.ndarray the same
    shape as dem['array'] (production_area.get_required_tree_root_zone_
    mask_utm()'s own output -- already on dem's own grid, no further
    alignment needed), or None. A True cell is HARD-EXCLUDED from
    candidacy before any factor is scored (see module docstring's
    "CANOPY EXCLUSION GATE" section) -- unlike the three *_union params
    above, there is no "gracefully degraded, neutral score" path for
    canopy here: None means "no gate applied at all" (this pure-logic
    core's own default, useful for callers/tests that don't care about
    canopy), not "checked, found none" -- identify_tree_zone_candidates()
    always supplies a real mask (its own canopy fetch is mandatory,
    non-degrading, same as production_area.py's).

    Returns one entry per resulting tree-zone candidate patch, ranked
    best-first:
        {
            'id': int,
            'rank': int,
            'polygon_utm': shapely Polygon/MultiPolygon,
            'geometry_wgs84': GeoJSON geometry dict,
            'area_acres': float,
            'tree_suitability_score': float,   # 0-100
            'soil_marginality_factor': float,  # 0-1
            'slope_factor': float,             # 0-1
            'hydric_overlap_factor': float,    # 0-1
            'stream_proximity_factor': float,  # 0-1
            'avg_slope_pct': float,
            'soil_marginality_data_available': bool,
            'hydric_data_available': bool,
            'stream_data_available': bool,
        }
    """
    if search_space_utm is None or search_space_utm.is_empty:
        return []

    array = dem["array"]
    rows, cols = array.shape
    valid = ~np.isnan(array)
    slope_pct_grid = compute_slope_percent(array, dem["resolution_meters"])

    search_space_prepared = prep(search_space_utm)
    prime_prepared = (
        prep(prime_farmland_union) if prime_farmland_union is not None and not prime_farmland_union.is_empty else None
    )
    hydric_prepared = prep(hydric_union) if hydric_union is not None and not hydric_union.is_empty else None
    stream_geom = stream_union if stream_union is not None and not stream_union.is_empty else None

    composite_grid = np.full((rows, cols), np.nan, dtype=np.float64)
    slope_factor_grid = np.full((rows, cols), np.nan, dtype=np.float64)
    soil_marginality_grid = np.full((rows, cols), np.nan, dtype=np.float64)
    hydric_overlap_grid = np.full((rows, cols), np.nan, dtype=np.float64)
    stream_proximity_grid = np.full((rows, cols), np.nan, dtype=np.float64)

    for r in range(rows):
        for c in range(cols):
            if not valid[r, c]:
                continue
            point = Point(pixel_center_xy(dem, r, c))
            if not search_space_prepared.contains(point):
                continue
            if tree_root_zone_mask_utm is not None and tree_root_zone_mask_utm[r, c]:
                # Already under (or within TREE_ZONE_CANOPY_BUFFER_METERS
                # of) real, existing tree canopy -- HARD-excluded before
                # scoring, same as being outside the search space
                # entirely (see this function's own docstring and the
                # module docstring's "CANOPY EXCLUSION GATE" section).
                continue

            raw_slope = float(slope_pct_grid[r, c]) if not np.isnan(slope_pct_grid[r, c]) else 0.0
            sf = _slope_factor(raw_slope, slope_reference_pct)

            if not prime_farmland_data_available:
                sm = _NEUTRAL_FACTOR_VALUE
            elif prime_prepared is not None and prime_prepared.contains(point):
                sm = 0.0
            else:
                sm = 1.0

            if not hydric_data_available:
                ho = _NEUTRAL_FACTOR_VALUE
            elif hydric_prepared is not None and hydric_prepared.contains(point):
                ho = 1.0
            else:
                ho = 0.0

            if not stream_data_available:
                sp = _NEUTRAL_FACTOR_VALUE
            else:
                distance_m = point.distance(stream_geom) if stream_geom is not None else None
                sp = _stream_proximity_factor(distance_m, stream_proximity_reference_meters)

            composite = (
                HYDRIC_OVERLAP_FACTOR_WEIGHT * ho
                + SLOPE_FACTOR_WEIGHT * sf
                + SOIL_MARGINALITY_FACTOR_WEIGHT * sm
                + STREAM_PROXIMITY_FACTOR_WEIGHT * sp
            )

            slope_factor_grid[r, c] = sf
            soil_marginality_grid[r, c] = sm
            hydric_overlap_grid[r, c] = ho
            stream_proximity_grid[r, c] = sp
            composite_grid[r, c] = composite

    candidate_mask = (~np.isnan(composite_grid)) & (composite_grid >= min_score / SUITABILITY_SCORE_SCALE)
    labels, num_components = connected_components(candidate_mask)

    px, py = dem["resolution_meters"]
    patches = []

    for component_id in range(num_components):
        cells = [(int(r), int(c)) for r, c in np.argwhere(labels == component_id)]

        squares = []
        for r, c in cells:
            x, y = pixel_center_xy(dem, r, c)
            squares.append(box(x - px / 2, y - py / 2, x + px / 2, y + py / 2))

        footprint = _polygonal_parts(unary_union(squares).intersection(search_space_utm).intersection(boundary_polygon_utm))
        if footprint is None or footprint.is_empty:
            continue

        area_acres = footprint.area / SQUARE_METERS_PER_ACRE
        if area_acres < min_area_acres:
            continue

        avg_slope_pct = float(np.mean([float(slope_pct_grid[r, c]) if not np.isnan(slope_pct_grid[r, c]) else 0.0 for r, c in cells]))
        slope_factor = float(np.mean([slope_factor_grid[r, c] for r, c in cells]))
        soil_marginality_factor = float(np.mean([soil_marginality_grid[r, c] for r, c in cells]))
        hydric_overlap_factor = float(np.mean([hydric_overlap_grid[r, c] for r, c in cells]))
        stream_proximity_factor = float(np.mean([stream_proximity_grid[r, c] for r, c in cells]))

        # Recomputed once from the patch-level averaged factors (not by
        # re-averaging per-cell composite scores) -- same reasoning
        # production_suitability.py's own suitability_score follows. Since
        # the composite is a linear combination, the two are mathematically
        # identical anyway; this form is what makes the *_factor properties
        # and tree_suitability_score visibly consistent with each other.
        composite_score = (
            HYDRIC_OVERLAP_FACTOR_WEIGHT * hydric_overlap_factor
            + SLOPE_FACTOR_WEIGHT * slope_factor
            + SOIL_MARGINALITY_FACTOR_WEIGHT * soil_marginality_factor
            + STREAM_PROXIMITY_FACTOR_WEIGHT * stream_proximity_factor
        )

        geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(footprint))

        patches.append(
            {
                "id": component_id,
                "polygon_utm": footprint,
                "geometry_wgs84": geometry_wgs84,
                "area_acres": round(area_acres, 2),
                "tree_suitability_score": round(composite_score * SUITABILITY_SCORE_SCALE, 1),
                "soil_marginality_factor": round(soil_marginality_factor, 3),
                "slope_factor": round(slope_factor, 3),
                "hydric_overlap_factor": round(hydric_overlap_factor, 3),
                "stream_proximity_factor": round(stream_proximity_factor, 3),
                "avg_slope_pct": round(avg_slope_pct, 1),
                "soil_marginality_data_available": prime_farmland_data_available,
                "hydric_data_available": hydric_data_available,
                "stream_data_available": stream_data_available,
            }
        )

    patches.sort(key=lambda p: -p["tree_suitability_score"])
    for rank, patch in enumerate(patches, start=1):
        patch["rank"] = rank

    return patches


def _fetch_prime_farmland_union(boundary_coordinates: list[tuple[float, float]], dem: dict) -> Optional[object]:
    """
    Real SSURGO polygon geometry for every map unit SSURGO classifies as
    prime (or conditionally prime) farmland (soil_data.is_prime_farmland(),
    the same field/logic solar_suitability.py already uses to FLAG -- not
    score -- a prime-farmland conflict), within the property boundary,
    reprojected into dem['crs']. This is soil_marginality_factor's INPUT
    geometry (inverted to LOW tree suitability -- see module docstring).
    Returns None if nothing prime was found (the common, clean case, not
    an error).
    """
    wkt_polygon = coordinates_to_wkt_polygon(boundary_coordinates)
    classifications = get_farmland_classification_for_polygon(wkt_polygon)
    prime_mukeys = {c["mukey"] for c in classifications if is_prime_farmland(c.get("farmland_classification"))}
    if not prime_mukeys:
        return None

    geometries_by_mukey = get_soil_geometries_for_polygon(wkt_polygon)
    pieces = [
        shape(transform_geom("EPSG:4326", dem["crs"], geometries_by_mukey[mukey]))
        for mukey in prime_mukeys
        if mukey in geometries_by_mukey
    ]
    return unary_union(pieces) if pieces else None


def _fetch_hydric_soil_union(boundary_coordinates: list[tuple[float, float]], dem: dict) -> Optional[object]:
    """
    Real SSURGO hydric-soil polygon geometry within the property boundary --
    the SAME shared mukey-selection logic production_suitability.py's own
    soil-carving already uses (soil_data.hydric_disqualifying_mukeys(), same
    MIN_HYDRIC_COMPONENT_PCT_TO_EXCLUDE threshold), just fetched once for
    the whole boundary rather than per production-patch (there's no
    per-patch context here -- this feature scores leftover land generally).
    Returns None if nothing hydric was found.
    """
    wkt_polygon = coordinates_to_wkt_polygon(boundary_coordinates)
    soil_components = get_soil_data_for_polygon(wkt_polygon)
    disqualifying_mukeys = hydric_disqualifying_mukeys(soil_components)
    if not disqualifying_mukeys:
        return None

    geometries_by_mukey = get_soil_geometries_for_polygon(wkt_polygon)
    pieces = [
        shape(transform_geom("EPSG:4326", dem["crs"], geometries_by_mukey[mukey]))
        for mukey in disqualifying_mukeys
        if mukey in geometries_by_mukey
    ]
    return unary_union(pieces) if pieces else None


def _fetch_stream_union(boundary_coordinates: list[tuple[float, float]], dem: dict) -> Optional[object]:
    """
    Real NHD stream/flowline geometry (streams only -- NOT standing water
    bodies; see module docstring for why this is independent of
    hydric_overlap_factor) near the property boundary, reprojected into
    dem['crs']. Returns None if no streams were found nearby.
    """
    water_features = get_water_features_for_boundary(boundary_coordinates)
    pieces = []
    for feature in water_features["streams"]:
        geometry = feature.get("geometry")
        if geometry is None:
            continue
        pieces.append(shape(transform_geom("EPSG:4326", dem["crs"], geometry)))
    return unary_union(pieces) if pieces else None


def _data_availability_note(
    soil_marginality_data_available: bool, hydric_data_available: bool, stream_data_available: bool
) -> str:
    notes = []
    if not soil_marginality_data_available:
        notes.append(
            "SSURGO Farmland Classification data was not available for this run, so "
            "soil_marginality_factor defaulted to a neutral value rather than a real measurement."
        )
    if not hydric_data_available:
        notes.append(
            "SSURGO hydric-soil data was not available for this run, so hydric_overlap_factor "
            "defaulted to a neutral value rather than a real measurement."
        )
    if not stream_data_available:
        notes.append(
            "NHD stream data was not available for this run, so stream_proximity_factor defaulted "
            "to a neutral value rather than a real measurement."
        )
    return (" ".join(notes) + " ") if notes else ""


def tree_zones_to_geojson(patches: list[dict]) -> dict:
    """Wraps score_tree_search_space() output as a schema-conformant
    GeoJSON FeatureCollection (layer="tree_zone_candidate")."""
    features = []
    for patch in patches:
        confidence_notes = TREE_ZONE_CONFIDENCE_NOTES_TEMPLATE.format(
            hydric_weight=HYDRIC_OVERLAP_FACTOR_WEIGHT,
            slope_weight=SLOPE_FACTOR_WEIGHT,
            soil_weight=SOIL_MARGINALITY_FACTOR_WEIGHT,
            stream_weight=STREAM_PROXIMITY_FACTOR_WEIGHT,
            data_availability_note=_data_availability_note(
                patch["soil_marginality_data_available"], patch["hydric_data_available"], patch["stream_data_available"]
            ),
        )

        features.append(
            make_feature(
                feature_id=f"tree-zone-candidate-{patch['id']}",
                geometry=patch["geometry_wgs84"],
                layer="tree_zone_candidate",
                label=f"Tree zone candidate {patch['id']} (rank {patch['rank']})",
                confidence=CONFIDENCE_LOW,
                confidence_notes=confidence_notes,
                extra_properties={
                    "area_acres": patch["area_acres"],
                    "tree_suitability_score": patch["tree_suitability_score"],
                    "soil_marginality_factor": patch["soil_marginality_factor"],
                    "slope_factor": patch["slope_factor"],
                    "hydric_overlap_factor": patch["hydric_overlap_factor"],
                    "stream_proximity_factor": patch["stream_proximity_factor"],
                    "avg_slope_pct": patch["avg_slope_pct"],
                    "rank": patch["rank"],
                    "soil_marginality_data_available": patch["soil_marginality_data_available"],
                    "hydric_data_available": patch["hydric_data_available"],
                    "stream_data_available": patch["stream_data_available"],
                },
            )
        )

    return make_feature_collection(features)


def _search_space_to_geojson(search_space_utm, crs: str) -> dict:
    """Diagnostic-layer wrapper (layer="tree_search_space_diagnostic") for
    Step 1's own output -- lets Step 1 (the search space) be inspected and
    sanity-checked independently of Steps 2-3 (scoring/thresholding), same
    "expose the intermediate stage, not just the final answer" reasoning
    water_candidate_zones.py's own valleys_geojson/production_areas_geojson
    diagnostics already use."""
    if search_space_utm is None or search_space_utm.is_empty:
        return make_feature_collection([])

    geometry_wgs84 = transform_geom(crs, "EPSG:4326", mapping(search_space_utm))
    feature = make_feature(
        feature_id="tree-search-space",
        geometry=geometry_wgs84,
        layer="tree_search_space_diagnostic",
        label="Tree suitability search space (leftover, non-claimed land)",
        confidence=CONFIDENCE_LOW,
        confidence_notes=TREE_SEARCH_SPACE_CONFIDENCE_NOTES,
        extra_properties={},
    )
    return make_feature_collection([feature])


def identify_tree_zone_candidates(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    **score_kwargs,
) -> dict:
    """
    Full pipeline entry point: fetches the DEM (unless one is passed in),
    computes the Step 1 search space from EVERY current production
    candidate plus the SINGLE selected water zone and SINGLE selected road
    corridor on this property, fetches Step 2's MANDATORY canopy
    exclusion mask (production_area.get_required_tree_root_zone_mask_utm()
    -- raises, does NOT degrade, on a fetch failure) plus Step 2's
    soil/stream geometry (each of THOSE three degrading independently and
    gracefully -- same pattern as every other network-backed layer in
    this pipeline), scores and thresholds the result, and returns:

        {
            'zones_geojson': FeatureCollection,          # layer="tree_zone_candidate" -- the deliverable
            'search_space_geojson': FeatureCollection,    # layer="tree_search_space_diagnostic" -- Step 1 diagnostic
            'search_space_acres': float,
            'claimed_acres': float,                       # production+selected-water+selected-road union's own
                                                             # area (roads ~0, zero-width)
            'boundary_acres': float,
            'patches': list[dict],                        # score_tree_search_space()'s own raw output
        }

    production_area_ceiling.py's own SSURGO fetch (soil carving, reused
    unchanged from production_suitability.py via its cells_by_patch_id
    parameter), water_suitability.py's own per-zone SSURGO/NHD fetches,
    and road_corridors.py's own several fetches (floodplain/erosion/farm
    roads) are all reused via their own full entry points
    (identify_optimized_production_areas(), identify_water_suitability(),
    identify_road_corridor_candidates()) rather than reimplemented here --
    this guarantees Step 1's "claimed" geometry is exactly what those
    layers currently, independently report as their own candidates (the
    OPTIMIZED, ceiling-trimmed candidates for production; for water/road,
    their own SELECTED candidate specifically -- see
    water_suitability.select_optimal_water_zone()/
    road_corridors.select_optimal_road_corridor()), not a re-derived
    approximation of it.
    """
    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    boundary_xs, boundary_ys = warp_transform(
        "EPSG:4326",
        dem["crs"],
        [pt[0] for pt in boundary_coordinates],
        [pt[1] for pt in boundary_coordinates],
    )
    boundary_polygon_utm = Polygon(zip(boundary_xs, boundary_ys))

    # --- Step 1 inputs: the OPTIMIZED (ceiling-trimmed) production-zone
    # output (production_area_ceiling.identify_optimized_production_areas(),
    # not production_area.identify_production_areas()'s full, un-trimmed
    # candidates -- see module docstring), plus only the SINGLE selected
    # water zone and SINGLE selected road corridor from their own
    # optimized-selection entry points. Per product decision, this app
    # targets small farms only: one well-suited water zone / road corridor
    # is sufficient, so only that one candidate's own geometry counts as
    # "claimed" for each. Each geometry claimed here is that zone's own
    # OPTIMIZED/render form, NOT its raw cell/DEM-walk footprint -- see
    # this module's own "GEOMETRY FORM CLAIMED" docstring section above
    # for why (production_area.py's own cluster_and_gate() docstring for
    # render_fill_polygon_utm itself). ---
    production_result = identify_optimized_production_areas(boundary_coordinates, dem=dem)
    production_polygons_utm = [p["render_fill_polygon_utm"] for p in production_result["scored_patches"]]

    water_result = identify_water_suitability(boundary_coordinates, dem=dem)
    selected_water_zone = water_result["selected_water_zone"]
    water_polygons_utm = [selected_water_zone["render_fill_polygon_utm"]] if selected_water_zone else []

    # identify_road_corridor_candidates() now requires a real anchor_lon_lat
    # to generate any routes at all (see road_corridors.py's own module
    # docstring) -- there's no real one available in this module's own
    # context yet (a genuine product decision, not derivable from the
    # boundary alone), so this borrows render_layout_map.py's TEMPORARY
    # placeholder reference-property anchor purely to unblock testing/live
    # runs, same as render_layout_map.py's own call. _smooth_line_for_
    # render() is render_layout_map.py's own display-smoothing transform
    # for a road corridor's real, per-DEM-cell-stairstepped line_utm (see
    # this module's "GEOMETRY FORM CLAIMED" section above) -- reused
    # directly here rather than reimplemented, so there's exactly one
    # definition of "the road's optimized alignment" for both the map and
    # this exclusion. Both imported locally rather than at module level
    # so importing tree_zone_candidates.py doesn't also eagerly pull in
    # render_layout_map.py's own heavier rendering dependencies
    # (matplotlib/contextily/xyzservices) for callers that never actually
    # render a map.
    from render_layout_map import _PLACEHOLDER_REFERENCE_PROPERTY_ANCHOR_LON_LAT, _smooth_line_for_render

    road_result = identify_road_corridor_candidates(
        boundary_coordinates, dem=dem, anchor_lon_lat=_PLACEHOLDER_REFERENCE_PROPERTY_ANCHOR_LON_LAT
    )
    selected_road_corridor = road_result["selected_road_corridor"]
    road_lines_utm = (
        [_smooth_line_for_render(selected_road_corridor["line_utm"])] if selected_road_corridor else []
    )

    search_space, claimed_union = compute_tree_search_space(
        boundary_polygon_utm, production_polygons_utm, water_polygons_utm, road_lines_utm
    )

    # --- Step 2's own CANOPY EXCLUSION GATE (see module docstring):
    # MANDATORY, non-degrading -- the SAME shared fetch-or-raise helper
    # production_area.identify_production_areas()/production_area_
    # ceiling.identify_optimized_production_areas() already call for
    # their own canopy gates, at this module's own TREE_ZONE_CANOPY_
    # BUFFER_METERS (== production's own TREE_ROOT_ZONE_BUFFER_METERS,
    # 10ft, unchanged). A canopy fetch failure here propagates up
    # UNCAUGHT, same hard-fail behavior as every other mandatory-canopy
    # entry point in this pipeline -- callers are expected to let this
    # raise, not catch and degrade it. Fetched fresh here rather than
    # reused from production_result above (that result's own internal
    # mask isn't part of its returned dict) -- one more independent
    # canopy fetch per call, same "each optimize/select call below
    # re-derives its own upstream dependencies" pattern render_layout_
    # map.py's own fetch_layout_layers() already documents. ---
    tree_root_zone_mask_utm = get_required_tree_root_zone_mask_utm(
        boundary_polygon_utm, dem, buffer_meters=TREE_ZONE_CANOPY_BUFFER_METERS
    )

    # --- Step 2's remaining inputs: soil/stream geometry, each degrading
    # independently and gracefully -- same pattern as every other
    # network-backed layer in this pipeline (e.g. road_corridors.py's
    # several optional fetches). ---
    prime_farmland_union: Optional[object] = None
    prime_farmland_data_available = True
    try:
        prime_farmland_union = _fetch_prime_farmland_union(boundary_coordinates, dem)
    except Exception:
        prime_farmland_data_available = False

    hydric_union: Optional[object] = None
    hydric_data_available = True
    try:
        hydric_union = _fetch_hydric_soil_union(boundary_coordinates, dem)
    except Exception:
        hydric_data_available = False

    stream_union: Optional[object] = None
    stream_data_available = True
    try:
        stream_union = _fetch_stream_union(boundary_coordinates, dem)
    except Exception:
        stream_data_available = False

    patches = score_tree_search_space(
        dem,
        search_space,
        boundary_polygon_utm,
        prime_farmland_union=prime_farmland_union,
        prime_farmland_data_available=prime_farmland_data_available,
        hydric_union=hydric_union,
        hydric_data_available=hydric_data_available,
        stream_union=stream_union,
        stream_data_available=stream_data_available,
        tree_root_zone_mask_utm=tree_root_zone_mask_utm,
        **score_kwargs,
    )

    search_space_acres = (
        search_space.area / SQUARE_METERS_PER_ACRE if search_space is not None and not search_space.is_empty else 0.0
    )
    claimed_acres = (
        claimed_union.area / SQUARE_METERS_PER_ACRE if claimed_union is not None and not claimed_union.is_empty else 0.0
    )

    return {
        "zones_geojson": tree_zones_to_geojson(patches),
        "search_space_geojson": _search_space_to_geojson(search_space, dem["crs"]),
        "search_space_acres": round(search_space_acres, 2),
        "claimed_acres": round(claimed_acres, 2),
        "boundary_acres": round(boundary_polygon_utm.area / SQUARE_METERS_PER_ACRE, 2),
        "patches": patches,
    }


def summarize_tree_zone_candidates(result: dict) -> str:
    lines = [
        f"Search space (leftover, non-claimed land): {result['search_space_acres']} acres of "
        f"{result['boundary_acres']} total ({result['claimed_acres']} acres already claimed by "
        "production/water/road candidates)."
    ]

    features = result["zones_geojson"]["features"]
    if not features:
        lines.append(
            "No tree zone candidates identified (nothing in the leftover search space cleared "
            "the minimum suitability threshold)."
        )
        return "\n".join(lines)

    lines.append(f"Tree zone candidates: {len(features)}")
    for feature in sorted(features, key=lambda f: f["properties"]["rank"]):
        props = feature["properties"]
        lines.append(
            f"  - Rank {props['rank']}: score {props['tree_suitability_score']}/100, {props['area_acres']} acres "
            f"(soil_marginality={props['soil_marginality_factor']}, slope={props['slope_factor']}, "
            f"hydric_overlap={props['hydric_overlap_factor']}, stream_proximity={props['stream_proximity_factor']})"
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

    print("Identifying tree zone candidates for property boundary...\n")

    try:
        result = identify_tree_zone_candidates(property_boundary)
        print(summarize_tree_zone_candidates(result))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map services and USDA's Soil Data Access — not a fully "
            "sandboxed environment."
        )
