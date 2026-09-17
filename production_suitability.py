"""
production_suitability.py

STEP 4 of the consolidated production-zone pipeline (this file /
production_area.py / production_area_ceiling.py together) -- adds a
suitability RANKING to the production-zone clusters STEP 3
(production_area.cluster_and_gate()) has already identified. This is
PURELY DESCRIPTIVE/ADVISORY metadata (e.g. ranking which surviving
fragment is worth prioritizing on a fragmented result, for
report_generator.py's eventual narrative) -- nothing filters or gates on
suitability_score; survival was already decided by STEP 3's pure area
check.

This module is intentionally the one piece of the pipeline that is fully
offline/network-free and has NO dependency on production_area.py or
production_area_ceiling.py: it takes already-computed clusters (each
carrying its own 'cells') plus STEP 1's already-computed per-cell
slope/aspect scores and soil-carved bookkeeping (production_area.
compute_step1_eligible_cells()'s return dict, duck-typed here, not
imported as a type), and does no recomputation of slope, aspect, or
hydric soil at all:

    slope_factor / aspect_factor -- averaged directly from STEP 1's
        ALREADY-COMPUTED per-cell scores over each cluster's own cells.
        This is the one thing that changed from the pre-consolidation
        architecture: there used to be a SECOND, slightly different
        implementation here (a per-patch list-average recomputing slope/
        aspect from scratch) -- that's gone. One implementation (STEP 1's),
        reused here.
    shape_factor -- Polsby-Popper compactness of the cluster's own cell
        footprint, and the one thing that genuinely cannot be computed
        before clustering exists (a cluster's own shape has no meaning
        before STEP 3 produces it), so it's computed fresh here via
        _shape_factor().

        IT WAS size_factor, HALF ACREAGE AND HALF SHAPE, AND THE ACREAGE
        HALF IS GONE. That half normalised a cluster's acreage against a
        reference maximum this module picked (10 acres), so what it
        measured was "how big is this relative to a ceiling we chose" --
        and on a 13-acre parcel that is a penalty for the PARCEL's size
        levied on every block, not a statement about any block's quality.
        It was a large part of why real blocks on the reference property
        scored 34-50 with nothing wrong with them. Compactness is a
        different kind of claim and stays: an irregular sliver is harder
        to work than a compact block of the SAME acreage -- turning room,
        equipment passes -- and that is a fact about the ground rather
        than about the reference we measured it against.

        RENAMED WITH THE CHANGE, deliberately: a factor called size_
        factor that measures only shape is a name that lies to the next
        reader, and its two published sub-scores (area_score,
        compactness_score) went with the blend they decomposed -- a
        factor with one input has no halves to report, and compactness_
        score would now be the same number as shape_factor under a second
        name.
    soil_factor -- the drainage class of the map unit under the cluster,
        scored by _soil_factor(). NEW on this branch, and the first thing
        in production scoring that reads soil at all: until it landed, a
        block on well-drained Gilpin and a block on somewhat poorly
        drained Ernest scored identically whenever their slope and shape
        matched, which is not a distinction any farmer would let stand.
        The class arrives as data (drainage_class_by_patch_id), computed
        once per run by production_area_ceiling._soil_attribution() off
        ParcelData's own SSURGO rows -- this module still fetches nothing
        and still imports nothing from the other two.
    soil_carved_acres/pct -- now simple bookkeeping (STEP 1's
        soil_carved_acres_by_cell/soil_carved_pct_by_cell arrays, looked up
        per cluster via its own cells), not a second carving pass. Hydric
        soil was already excluded at the CELL level, before clustering, so
        there is nothing left to carve or split here -- the old
        continuous-geometry carving/splitting machinery
        (_carve_soil_from_patch(), _fetch_disqualifying_soil_union(), the
        lettered-sub-id splitting) is gone entirely, superseded by STEP 1.

WHAT A LOW soil_factor MEANS, AND IT IS NOT A VERDICT ON THE GROUND. Soil
is the LAST step of the Keyline Scale of Permanence because it is the most
easily IMPROVED: drainage, organic matter and structure are what a
management decision can actually move, which is exactly why they sit below
climate, landform and water in the sequence. So a block scoring low here
is not bad ground -- it is ground that needs work, and the factor is a
measure of HOW MUCH. Real hydric/wetland soil is the separate, absolute
case and is not scored at all: it was excluded cell-by-cell at STEP 1
before any cluster formed.
"""

import math

import numpy as np
from shapely.geometry import box
from shapely.ops import unary_union

from raster_grid import pixel_center_xy

# --- composite weights (must sum to 1.0). CONFIGURABLE -- tune against a
# real property once ground-truthed. Aspect is deliberately the smallest
# weight -- general production suitability cares far less about compass
# orientation than solar siting did.
#
# FOUR FACTORS NOW, NOT THREE, and the other three were rescaled
# PROPORTIONALLY to make room for soil: 0.55 / 0.30 / 0.15 each multiplied
# by 0.80, which is the same three factors in the same relative shares.
#
# NOT slope-held-at-0.55-with-soil-taken-out-of-the-other-two. Slope's
# dominance was argued as a RANKING (gentle ground matters most), never as
# a floor, so there is no reason it alone should be exempt from making
# room. And taking 0.20 out of shape and aspect between them would leave
# aspect at 0.08 -- a weight that can move a finished score by eight points
# across its entire range, which is a factor that is published rather than
# consulted.
SLOPE_FACTOR_WEIGHT = 0.44
SHAPE_FACTOR_WEIGHT = 0.24
ASPECT_FACTOR_WEIGHT = 0.12
SOIL_FACTOR_WEIGHT = 0.20

_WEIGHT_SUM = (
    SLOPE_FACTOR_WEIGHT + SHAPE_FACTOR_WEIGHT + ASPECT_FACTOR_WEIGHT + SOIL_FACTOR_WEIGHT
)
assert math.isclose(_WEIGHT_SUM, 1.0, abs_tol=1e-6), f"suitability factor weights must sum to 1.0, got {_WEIGHT_SUM}"

# suitability_score is reported on a 0-100 scale (composite of the 0-1
# factors below, rounded to 1 decimal).
SUITABILITY_SCORE_SCALE = 100

# THE SIZE SUB-WEIGHTS AND REFERENCE_MAX_AREA_ACRES USED TO BE HERE, and
# they are gone rather than set to zero: shape_factor has one input, so it
# has no sub-weights to carry, and no caller can pass a reference acreage
# to a scorer that no longer normalises against one. See the module
# docstring for the argument.

# The value every factor takes when the thing it measures could not be
# MEASURED -- neither a penalty nor a reward, so an unsurveyed block
# neither out-ranks nor loses to a surveyed one on a check that did not
# run. tree_zone_candidates._NEUTRAL_FACTOR_VALUE is the same constant for
# the same reason, and that branch established the trap this one inherits:
# a neutral default is indistinguishable from a genuinely measured 0.5
# unless an AVAILABILITY FLAG travels beside it. soil_available is that
# flag here, exactly as aspect_available already is for aspect_factor.
_NEUTRAL_FACTOR_VALUE = 0.5

# --- drainage class -> 0-1 soil factor. CONFIGURABLE -- tune against a
# real property once ground-truthed, like every other threshold in this
# pipeline.
#
# SSURGO's `drainagecl`, all seven classes it publishes. Drainage ALONE is
# the whole factor: it is the field the panel already shows, it is the most
# directly relevant single statement SSURGO makes about production ground,
# and it is one measurement rather than a blend. Prime-farmland status and
# a ksat/hydrologic-group composite were both considered and both rejected
# -- they are second signals about the same ground, and a blend of them
# would partly re-count what the hydric gate has already excluded outright.
#
# THE SPACING IS DELIBERATELY UNEVEN, and even spacing is the thing it is
# avoiding. Seven classes at equal steps put "moderately well drained" at
# 0.50 and "well drained" at 0.67, which reads as a third of a factor
# between two classes a grower would treat as fine and fine. For PRODUCTION
# the useful range is COMPRESSED AT THE TOP -- well drained, somewhat
# excessively drained and moderately well drained are all workable ground,
# separated by a few points -- and SPREAD AT THE BOTTOM, where the
# difference between somewhat poorly and very poorly drained is the
# difference between a wet spring and a field you cannot get onto.
# production_area_ceiling._SCORE_BANDS is the precedent: its "poor" band
# spans forty points because nothing a reader decides changes between 5
# and 35.
#
# EXCESSIVELY DRAINED IS NOT THE TOP. It sits below somewhat excessively
# drained because past a point drainage becomes droughtiness: water leaves
# faster than a crop can use it, and the block needs irrigation or organic
# matter rather than tile. The scale is production suitability, not
# dryness.
#
# Keyed on the lower-cased class, so "Well drained" and "well drained" --
# both of which appear in real SSURGO exports -- are one key.
DRAINAGE_CLASS_FACTORS = {
    "well drained": 1.00,
    "somewhat excessively drained": 0.90,
    "moderately well drained": 0.85,
    "excessively drained": 0.80,
    "somewhat poorly drained": 0.55,
    "poorly drained": 0.25,
    "very poorly drained": 0.05,
}

# Polsby-Popper compactness (4*pi*area/perimeter^2) of a perfect square is
# exactly pi/4 (~0.785), not 1.0 -- and for an axis-aligned raster
# footprint a solid square block of cells is the most compact shape
# achievable. Dividing by this ceiling rescales so a best-case compact
# block reads as 1.0.
_SQUARE_COMPACTNESS = math.pi / 4

PRODUCTION_SUITABILITY_CONFIDENCE_NOTES_TEMPLATE = (
    "This ADDS a suitability ranking to production-zone clusters that survived STEP 1's slope + "
    "hydric-soil cell-level gates and STEP 3's area survival check -- it does not change which ground "
    "counts as a candidate or its boundary. suitability_score (0-100) is a weighted composite of FOUR "
    "independently-stored 0-1 factors: slope_factor (weight {slope_weight}, averaged from real per-cell "
    "DEM slope scores already computed before clustering), shape_factor (weight {shape_weight}, real "
    "geometry: Polsby-Popper compactness of the cluster's own cell footprint -- an irregular sliver "
    "scores lower than a compact block of the same acreage, because it is harder to work rather than "
    "smaller), aspect_factor (weight {aspect_weight}, deliberately the smallest weight -- general "
    "production suitability cares far less about compass orientation than solar siting did -- "
    "{aspect_availability}), and soil_factor (weight {soil_weight}, from the SSURGO drainage class of "
    "the map unit under this block -- {soil_availability}). A LOW soil_factor IS NOT A VERDICT ON THIS "
    "GROUND: soil is the last step of the Keyline Scale of Permanence precisely because it is the most "
    "improvable, so the factor measures how much improvement this block needs, not whether it is worth "
    "farming. Real hydric/wetland soil is the separate, absolute case and is not scored at all: it was "
    "excluded cell-by-cell BEFORE this cluster was ever formed (see soil_carved_acres/pct for how much "
    "of this cluster's own originally slope-eligible source ground was excluded that way). This is a "
    "topographic + soil-based candidate zone, not a certainty -- ground-truth before committing to it. "
    "{soil_note}"
)

_SOIL_UNCARVED_NOTE = (
    "Checked against real SSURGO polygon geometry at the cell level before clustering: no disqualifying "
    "soil was found in this cluster's originally slope-eligible source ground, so it's reported "
    "unmodified (soil_carved_acres=0)."
)
_SOIL_CARVED_NOTE = (
    "Checked against real SSURGO polygon geometry at the cell level before clustering: {carved_acres} "
    "acres ({carved_pct}%) of this cluster's originally slope-eligible source ground (source patch id "
    "{source_id}) was disqualifying (hydric) soil and was excluded before this cluster was ever formed."
)
_SOIL_UNAVAILABLE_NOTE = (
    "No SSURGO geometry could be fetched (fetch failed, or the soil check was skipped), so hydric "
    "exclusion could NOT be verified here -- this cluster's geometry may still include disqualifying "
    "ground that wasn't caught. ESTIMATE, not a measurement."
)
_SOIL_FACTOR_AVAILABLE_NOTE = (
    "scored from the drainage class SSURGO publishes for the map unit covering most of this block"
)
_SOIL_FACTOR_OMITTED_NOTE = (
    "no drainage class could be read for this block (no soil survey coverage under it, or a class "
    "outside the seven SSURGO publishes), so soil_factor was defaulted to a neutral 0.5 and soil_"
    "available is false -- OMITTED, not measured, and the block is neither penalised nor rewarded for it"
)
_ASPECT_AVAILABLE_NOTE = "computed from real DEM-derived aspect (Horn's method) averaged across the cluster"
_ASPECT_OMITTED_NOTE = (
    "this cluster's ground was too flat for a well-defined downhill direction, so aspect_factor "
    "was defaulted to a neutral 1.0 (flat ground has no unfavorable orientation) -- OMITTED, not measured"
)


def _confidence_notes_for(patch: dict) -> str:
    """Builds the plain-language confidence_notes string for one scored
    cluster -- computed once here and attached directly to the patch dict
    itself (not just the eventual GeoJSON feature)."""
    if patch["soil_carved_acres"] > 0:
        soil_note = _SOIL_CARVED_NOTE.format(
            carved_acres=patch["soil_carved_acres"],
            carved_pct=patch["soil_carved_pct"],
            source_id=patch["source_patch_id"],
        )
    elif patch["soil_data_available"]:
        soil_note = _SOIL_UNCARVED_NOTE
    else:
        soil_note = _SOIL_UNAVAILABLE_NOTE

    return PRODUCTION_SUITABILITY_CONFIDENCE_NOTES_TEMPLATE.format(
        slope_weight=SLOPE_FACTOR_WEIGHT,
        shape_weight=SHAPE_FACTOR_WEIGHT,
        aspect_weight=ASPECT_FACTOR_WEIGHT,
        soil_weight=SOIL_FACTOR_WEIGHT,
        aspect_availability=_ASPECT_AVAILABLE_NOTE if patch["aspect_available"] else _ASPECT_OMITTED_NOTE,
        soil_availability=(
            _SOIL_FACTOR_AVAILABLE_NOTE if patch["soil_available"] else _SOIL_FACTOR_OMITTED_NOTE
        ),
        soil_note=soil_note,
    )


def _circular_mean_aspect_deg(aspect_values_deg: list[float]):
    """Mean compass bearing via vector averaging (a plain arithmetic mean
    of e.g. 350 deg and 10 deg would wrongly give 180 instead of 0).
    Returns None if every input is undefined (an all-flat cluster)."""
    valid = [a for a in aspect_values_deg if not math.isnan(a)]
    if not valid:
        return None
    sin_sum = sum(math.sin(math.radians(a)) for a in valid)
    cos_sum = sum(math.cos(math.radians(a)) for a in valid)
    return math.degrees(math.atan2(sin_sum, cos_sum)) % 360


def _shape_factor(cells: list[tuple[int, int]], dem: dict) -> float:
    """0-1 shape-compactness score for a cluster's own constituent DEM
    cells (NOT its convex-hull or carved-polygon footprint -- neither is
    guaranteed to reflect real fragmentation the way the actual
    constituent cells do). Builds the real footprint as a union of
    per-cell squares and scores it via Polsby-Popper
    (4*pi*area/perimeter^2), normalized against the most compact shape an
    axis-aligned raster footprint can achieve (a solid square block, see
    _SQUARE_COMPACTNESS)."""
    if not cells:
        return 0.0
    px, py = dem["resolution_meters"]
    squares = []
    for r, c in cells:
        x, y = pixel_center_xy(dem, r, c)
        squares.append(box(x - px / 2, y - py / 2, x + px / 2, y + py / 2))
    footprint = unary_union(squares)
    area = footprint.area
    perimeter = footprint.length
    if perimeter <= 0:
        return 0.0
    polsby_popper = 4 * math.pi * area / (perimeter**2)
    return max(0.0, min(1.0, polsby_popper / _SQUARE_COMPACTNESS))


def _soil_factor(drainage_class) -> tuple[float, bool]:
    """
    (soil_factor, soil_available) for one block, from the SSURGO drainage
    class of the map unit under it -- DRAINAGE_CLASS_FACTORS owns the
    mapping and the argument for its spacing.

    A MISSING CLASS IS NEUTRAL AND FLAGGED, never zero and never guessed.
    None arrives for real, on real ground: a parcel can sit outside soil
    survey coverage entirely, a block can fall on cells no map unit
    contains, and every map unit under a block can fall below production_
    area_ceiling.PATCH_SOIL_MIN_CELL_SHARE_PCT. All three are "we could not
    read the soil here", and all three must score the same as each other
    and differently from a measured mid-value -- hence _NEUTRAL_FACTOR_
    VALUE plus the False flag rather than a number alone. A zero would say
    the block sits on the worst drainage SSURGO publishes, which is a claim
    about someone's land that nobody made.

    A class SSURGO publishes that this table does not carry (an export
    spelling it differently, a class added later) takes the same neutral-
    and-flagged path for the same reason: the check ran, and it could not
    produce a score on this scale.
    """
    if not isinstance(drainage_class, str):
        return _NEUTRAL_FACTOR_VALUE, False
    value = DRAINAGE_CLASS_FACTORS.get(drainage_class.strip().lower())
    if value is None:
        return _NEUTRAL_FACTOR_VALUE, False
    return float(value), True


def score_production_areas(
    patches: list[dict],
    dem: dict,
    step1: dict,
    drainage_class_by_patch_id: dict = None,
    ungated_cell_factors=None,
) -> list[dict]:
    """
    STEP 4: advisory-only description of survivors -- see module docstring.

    patches is production_area.cluster_and_gate()'s own output (each entry
    must carry 'cells' and 'source_patch_id' -- UNCHANGED, this function
    does not alter membership, geometry, or which clusters exist; it only
    adds descriptive/ranking fields).

    ungated_cell_factors, when supplied, is called as
    ungated_cell_factors(row, col) -> (slope_factor, aspect_factor) for
    cells where STEP 1 recorded NO factor, and only for those.

    A CLUSTER'S CELLS ALWAYS HAVE ONE; A DRAWN BLOCK'S NEED NOT. STEP 1
    fills its per-cell factor arrays for cells that cleared every gate and
    leaves NaN everywhere else, which is complete for a cluster (it is made
    of eligible cells by construction) and incomplete for a block a PERSON
    drew -- they may draw across wooded, wet or steep ground, which is
    exactly what the caution system exists to warn them about. Averaged
    over the eligible cells alone, such a block would report the quality of
    the part of it that passed, captioned as the quality of the whole shape
    they drew. The caller supplies the fallback rather than this module
    computing it, for the same reason the drainage classes arrive as data:
    the two per-cell scores are production_area.per_cell_factors()' -- the
    SAME function STEP 1 filled its arrays with -- and this module imports
    nothing from there.

    A CELL WITH NO MEASURABLE SLOPE AT ALL (nodata under it) is left out of
    both averages rather than scored as flat; a block whose every cell is
    nodata raises ValueError, because there is no ground under it to
    describe.

    drainage_class_by_patch_id maps a patch's own integer id to the SSURGO
    drainage class of the map unit covering most of it, or to None where
    none could be read -- production_area_ceiling._soil_attribution() plus
    _patch_soil_fields() compute it once per run, off ParcelData's already-
    fetched rows, and hand it in here. DATA, NOT A LOOKUP THIS MODULE DOES:
    keeping it that way is what lets this module stay free of any import
    from production_area.py or production_area_ceiling.py (see the module
    docstring) and free of any fetch. Omitted entirely -- which is what a
    caller with no SSURGO rows in hand does -- every block scores the
    neutral soil_factor with soil_available False, which is the same answer
    a parcel outside survey coverage gets.

    step1 is production_area.compute_step1_eligible_cells()'s own return
    dict (duck-typed here -- this module imports nothing from
    production_area.py, see module docstring): its per_cell_slope_factor/
    per_cell_aspect_factor/slope_pct/aspect_deg/soil_carved_acres_by_cell/
    soil_carved_pct_by_cell arrays and soil_data_available flag are read
    directly, never recomputed.

    Returns a flat list of scored cluster dicts, each with these fields
    added:
        {
            'suitability_score': float,   # 0-100, ADVISORY ONLY -- nothing
                                            # gates on this
            'slope_factor': float,        # 0-1
            'shape_factor': float,        # 0-1, compactness alone
            'aspect_factor': float,       # 0-1
            'soil_factor': float,         # 0-1, drainage class alone
            'avg_slope_pct': float,
            'aspect_deg': Optional[float],
            'aspect_available': bool,
            'soil_available': bool,       # False -> soil_factor is the
                                            # neutral default, not a reading
            'soil_carved_acres': float,   # bookkeeping only -- see module docstring
            'soil_carved_pct': float,
            'soil_data_available': bool,
            'confidence_notes': str,
            'rank': int,                  # 1 = highest suitability_score
        }
    """
    per_cell_slope_factor = step1["per_cell_slope_factor"]
    per_cell_aspect_factor = step1["per_cell_aspect_factor"]
    slope_pct = step1["slope_pct"]
    aspect_deg = step1["aspect_deg"]
    soil_carved_acres_by_cell = step1["soil_carved_acres_by_cell"]
    soil_carved_pct_by_cell = step1["soil_carved_pct_by_cell"]
    soil_data_available = step1["soil_data_available"]
    drainage_by_id = dict(drainage_class_by_patch_id or {})

    for patch in patches:
        cells = patch["cells"]

        cell_slope_factors = []
        cell_aspect_factors = []
        cell_slopes = []
        for r, c in cells:
            slope_value = float(slope_pct[r, c])
            if math.isnan(slope_value):
                # No ground under this cell to measure -- a nodata hole in
                # the DEM. Left out of every average rather than counted as
                # flat, which is what including a 0.0 here would mean.
                continue
            cell_slopes.append(slope_value)
            cell_slope_factor = float(per_cell_slope_factor[r, c])
            cell_aspect_factor = float(per_cell_aspect_factor[r, c])
            if math.isnan(cell_slope_factor) and ungated_cell_factors is not None:
                cell_slope_factor, cell_aspect_factor = ungated_cell_factors(r, c)
            if not math.isnan(cell_slope_factor):
                cell_slope_factors.append(cell_slope_factor)
            if not math.isnan(cell_aspect_factor):
                cell_aspect_factors.append(cell_aspect_factor)

        if not cell_slopes or not cell_slope_factors:
            raise ValueError(
                f"block {patch.get('id')!r} covers {len(cells)} DEM cell(s) with no measurable slope "
                "under any of them, so there is nothing to score. Every factor here is an average over "
                "real ground, and an average over none of it is not a low score -- it is no answer."
            )

        slope_factor = float(np.mean(cell_slope_factors))
        # Flat ground has no defined aspect and no unfavourable orientation
        # -- the neutral-for-flat 1.0 STEP 1 itself applies, said once here
        # rather than per cell.
        aspect_factor = float(np.mean(cell_aspect_factors)) if cell_aspect_factors else 1.0

        avg_slope_pct = float(np.mean(cell_slopes))
        cell_aspects = [float(aspect_deg[r, c]) for r, c in cells]
        mean_aspect = _circular_mean_aspect_deg(cell_aspects)
        aspect_available = mean_aspect is not None

        shape_factor = _shape_factor(cells, dem)

        drainage_class = drainage_by_id.get(int(patch["id"])) if "id" in patch else None
        soil_factor, soil_available = _soil_factor(drainage_class)

        composite = (
            SLOPE_FACTOR_WEIGHT * slope_factor
            + SHAPE_FACTOR_WEIGHT * shape_factor
            + ASPECT_FACTOR_WEIGHT * aspect_factor
            + SOIL_FACTOR_WEIGHT * soil_factor
        )

        # THE SOIL-CARVED BOOKKEEPING IS A PROPERTY OF THE STEP 1 SOURCE
        # REGION a cluster was carved out of, looked up through any one of
        # its cells because every cell of a cluster traces back to the same
        # region. A block that came from no source region at all -- one a
        # person drew, which STEP 1 never saw -- has no carving to report,
        # and the figure standing at whatever the first cell under it
        # happened to inherit would be some other block's number printed
        # against this one.
        if int(patch.get("source_patch_id", -1)) < 0:
            soil_carved_acres = 0.0
            soil_carved_pct = 0.0
        else:
            first_r, first_c = cells[0]
            soil_carved_acres = soil_carved_acres_by_cell[first_r, first_c]
            soil_carved_pct = soil_carved_pct_by_cell[first_r, first_c]

        patch.update(
            {
                "suitability_score": round(composite * SUITABILITY_SCORE_SCALE, 1),
                "slope_factor": round(slope_factor, 3),
                "shape_factor": round(shape_factor, 3),
                "aspect_factor": round(aspect_factor, 3),
                "soil_factor": round(soil_factor, 3),
                "avg_slope_pct": round(avg_slope_pct, 1),
                "aspect_deg": round(mean_aspect, 1) if mean_aspect is not None else None,
                "aspect_available": aspect_available,
                "soil_available": soil_available,
                "soil_carved_acres": float(soil_carved_acres) if not np.isnan(soil_carved_acres) else 0.0,
                "soil_carved_pct": float(soil_carved_pct) if not np.isnan(soil_carved_pct) else 0.0,
                "soil_data_available": soil_data_available,
            }
        )
        patch["confidence_notes"] = _confidence_notes_for(patch)

    patches.sort(key=lambda p: -p["suitability_score"])
    for rank, patch in enumerate(patches, start=1):
        patch["rank"] = rank

    return patches


def production_suitability_to_geojson(scored_patches: list[dict]) -> dict:
    """Wraps score_production_areas() output as a schema-conformant
    GeoJSON FeatureCollection on the SAME layer production_area.py's own
    production_areas_to_geojson() uses ("production_area_candidate").

    CONSOLIDATED into wire_translation.py (as scored_production_areas_to_
    feature_collection) -- this name stays as the module's own entry
    point, forwarding to the single implementation kept there."""
    from wire_translation import scored_production_areas_to_feature_collection

    return scored_production_areas_to_feature_collection(scored_patches)


def summarize_production_area_suitability(scored_patches: list[dict]) -> str:
    if not scored_patches:
        return "No production-area candidates to score."

    lines = [f"Production-area suitability ranking ({len(scored_patches)} candidate(s)):"]
    for patch in sorted(scored_patches, key=lambda p: p["rank"]):
        carved_note = f", {patch['soil_carved_acres']} ac soil-carved from patch {patch['source_patch_id']}" if patch["soil_carved_acres"] > 0 else ""
        lines.append(
            f"  - Rank {patch['rank']}: patch {patch['id']}, score {patch['suitability_score']}/100 "
            f"(slope={patch['slope_factor']}, shape={patch['shape_factor']}, "
            f"aspect={patch['aspect_factor']}, soil={patch['soil_factor']}), "
            f"{patch['area_acres']} acres{carved_note}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    # Offline smoke test: this module is fully network-free post-
    # consolidation -- STEP 1 (production_area.py) already did the DEM/soil
    # work; this just describes clusters it's handed.
    import numpy as _np

    from production_area import compute_step1_eligible_cells, cluster_and_gate
    from shapely.geometry import box as _box

    size = 20
    array = _np.full((size, size), 100.0, dtype=_np.float32)
    dem = {
        "array": array,
        "resolution_meters": (5.0, 5.0),
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }
    boundary = _box(500000.0, 4500000.0 - size * 5.0, 500000.0 + size * 5.0, 4500000.0)

    step1 = compute_step1_eligible_cells(dem, boundary, disqualifying_soil_union_utm=None)
    patches = cluster_and_gate(step1["eligible_mask"], dem, boundary, step1)
    scored = score_production_areas(patches, dem, step1)
    print(summarize_production_area_suitability(scored))
