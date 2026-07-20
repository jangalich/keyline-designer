"""
production_suitability.py

Adds a suitability RANKING to production-zone candidates that
production_area.py has already identified — it does not change which
ground counts as a candidate or how its boundary is drawn (that stays
entirely production_area.py's job, untouched here). Given
identify_production_areas()'s patches, this scores each one against three
independently-computed, positively-weighted factors, plus a separate
pass/fail soil EXCLUSION check:

    slope_factor    -- flatter (within the low-slope mask that already
                        defined the patch) scores higher, from the same
                        DEM already fetched (production_area.py's own
                        compute_slope_percent()).
    size_factor      -- larger AND more compact/contiguous scores higher;
                        a large irregular sliver is penalized relative to
                        a compact block of the same acreage (see
                        _compactness_score()).
    aspect_factor    -- a minor factor (small weight, see
                        ASPECT_FACTOR_WEIGHT below): south-facing scores
                        higher, from the same DEM (terrain_metrics.py's
                        Horn-method aspect, already built for
                        solar_suitability.py). Aspect matters far less for
                        general production than it did for solar siting,
                        so it's included but deliberately not
                        heavily weighted.

    soil_exclusion   -- NOT a weighted score. SSURGO (already in the
                        pipeline) is checked only for conditions that
                        disqualify ground for production use REGARDLESS
                        of topography (hydric/wetland soil, permanently
                        saturated drainage — soil_data.is_disqualifying_
                        soil_condition()). A zone with merely mediocre-
                        but-workable soil scores identically to one with
                        excellent soil; only a genuinely disqualifying
                        condition has any effect (it drops the zone out
                        of ranking — see score_production_areas()).

Soil quality is deliberately NOT a positively-weighted scoring factor
here, unlike slope/size/aspect. Per Scale of Permanence sequencing, soil
is step 8 — the LAST step, and treated as the most IMPROVABLE factor of
the sequence (amendable, buildable, the one thing a grower can actually
change about a given piece of ground). Scoring zones higher for
currently-better soil would pull that step-8 judgment forward into what
should be a step-2 (Land Shape) decision about where production zones
sit at all. So SSURGO is used here the same way road_corridors.py already
uses it (is_erosion_prone()/is_hydric() — avoid, don't rank by quality),
not the way solar_suitability.py's prime-farmland flag or a graded score
would use it.

Each of the three scored factors, plus the exclusion check's own
pass/fail and reason, are stored independently in the output properties
(not just folded into one opaque number) alongside the weighted composite
suitability_score, so report narrative and any future scenario-selection
logic can explain WHY a zone scored/was excluded the way it did ("good
slope and shape, but excluded — hydric soil"), not just the final number.

    DEM (dem_data.py, already fetched for production_area.py)
        --> identify_production_areas() (production_area.py, UNCHANGED)
        --> [this module] per-patch slope/size/aspect scoring + soil
            exclusion check
        --> enriched production_area_candidate features (same layer,
            same zones -- just with suitability_score/*_factor/
            soil_exclusion_* properties added)

score_production_areas() is the pure scoring core: it takes an
already-computed DEM and patches list, plus optionally pre-fetched SSURGO
component rows per patch, and does no network I/O itself -- same
pure-core-vs-network-fetch split as water_candidate_zones.py and
solar_suitability.py, so the scoring math is unit-testable against a
synthetic DEM independent of whether SSURGO is reachable.
identify_production_area_suitability() is the fetch-and-score entry point.

This is a self-contained, standalone pass: it is NOT wired into
generate_full_report.py or report_generator.py's prompt in this pass (see
the module docstring conventions elsewhere in this codebase for that
"later pass" framing) -- validate the ranking on its own first, the same
way production_area.py's own diagnostic layer was validated before
water_candidate_zones.py or solar_suitability.py were built on top of it.
"""

import math
from typing import Optional

import numpy as np
from shapely.geometry import box
from shapely.ops import unary_union

from dem_data import get_dem_for_boundary
from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from production_area import MAX_PRODUCTION_SLOPE_PCT, compute_slope_percent, identify_production_areas
from raster_grid import connected_components, pixel_center_xy
from soil_data import coordinates_to_wkt_polygon, get_soil_data_for_polygon, is_disqualifying_soil_condition
from terrain_metrics import aspect_score, compute_slope_and_aspect

# --- composite weights (must sum to 1.0). CONFIGURABLE -- tune against a
# real property once ground-truthed; see README.md's roadmap note. Soil is
# deliberately NOT one of these -- see module docstring for why it's an
# exclusion check instead. Aspect is deliberately the smallest weight --
# general production suitability cares far less about compass orientation
# than solar siting did.
SLOPE_FACTOR_WEIGHT = 0.55
SIZE_FACTOR_WEIGHT = 0.30
ASPECT_FACTOR_WEIGHT = 0.15

_WEIGHT_SUM = SLOPE_FACTOR_WEIGHT + SIZE_FACTOR_WEIGHT + ASPECT_FACTOR_WEIGHT
assert math.isclose(_WEIGHT_SUM, 1.0, abs_tol=1e-6), f"suitability factor weights must sum to 1.0, got {_WEIGHT_SUM}"

# suitability_score is reported on a 0-100 scale (composite of the 0-1
# factors below, rounded to 1 decimal) -- same convention
# solar_suitability.py already uses for its own suitability_score, so a
# reader comparing the two layers doesn't have to remember two different
# scales. The three individual *_factor properties stay on their native
# 0-1 scale (documented per-property below), since those are meant to be
# read alongside each other and against the composite, not multiplied by
# 100 individually.
SUITABILITY_SCORE_SCALE = 100

# size_factor sub-weights (must sum to 1.0). CONFIGURABLE.
SIZE_AREA_SUBWEIGHT = 0.5
SIZE_SHAPE_SUBWEIGHT = 0.5

# Acreage at/above which the area component of size_factor maxes out at
# 1.0 -- bigger than this doesn't add further score, it's already a large,
# workable block. CONFIGURABLE -- tune to your own property's scale
# (production_area.py's own MIN_PRODUCTION_AREA_ACRES is 0.5; this is
# meant to sit well above that, not right at the noise floor).
REFERENCE_MAX_AREA_ACRES = 10.0

# Polsby-Popper compactness (4*pi*area/perimeter^2) of a perfect square is
# exactly pi/4 (~0.785), not 1.0 -- and for an axis-aligned raster
# footprint (this module's patch shapes, built from square DEM cells) a
# solid square block of cells is the most compact shape achievable, same
# as a real geometric square. Dividing by this ceiling rescales so that
# best-case compact block reads as 1.0 rather than topping out at ~0.785,
# while fragmented/elongated real shapes still fall well below it.
_SQUARE_COMPACTNESS = math.pi / 4

PRODUCTION_SUITABILITY_CONFIDENCE_NOTES_TEMPLATE = (
    "This ADDS a suitability ranking to production-zone candidates that were already "
    "identified by production_area.py's slope-only heuristic -- it does not change which "
    "ground counts as a candidate or its boundary (see that layer's own confidence_notes "
    "for the underlying detection caveats). suitability_score (0-100) is a weighted composite "
    "of THREE independently-stored 0-1 factors: slope_factor (weight {slope_weight}, real DEM "
    "slope), size_factor (weight {size_weight}, real geometry: acreage + Polsby-Popper "
    "compactness of the patch's own cell footprint -- a large irregular sliver scores lower "
    "than a compact block of the same acreage), and aspect_factor (weight {aspect_weight}, "
    "deliberately the smallest weight -- general production suitability cares far less about "
    "compass orientation than solar siting did -- {aspect_availability}). Soil quality is "
    "DELIBERATELY NOT one of these weighted factors -- per Scale of Permanence sequencing, "
    "soil is step 8, the last and most improvable step, so it shouldn't gate/rank where "
    "production zones go the way slope/size/aspect (Land Shape, step 2) do. Instead, SSURGO is "
    "checked only for conditions that disqualify ground for production use regardless of "
    "topography (hydric/wetland soil, permanently saturated drainage) -- see "
    "soil_exclusion_passed/soil_exclusion_reason, {soil_availability}. Weights are configurable "
    "module-level constants (production_suitability.py), not tuned against a real property yet."
)

_SOIL_AVAILABLE_NOTE = "checked against real SSURGO component data (hydricrating + drainagecl) for this patch's footprint"
_SOIL_ESTIMATED_NOTE = (
    "no SSURGO data was available for this patch's footprint (fetch failed or returned nothing), "
    "so the exclusion check could NOT be verified -- it defaults to passed (not excluded) rather "
    "than assumed clean, and this is stated here as an ESTIMATE, not a measurement"
)
_ASPECT_AVAILABLE_NOTE = "computed from real DEM-derived aspect (Horn's method) averaged across the patch"
_ASPECT_OMITTED_NOTE = (
    "this patch's ground was too flat for a well-defined downhill direction, so aspect_factor was "
    "defaulted to a neutral 1.0 (flat ground has no unfavorable orientation) -- OMITTED, not measured"
)


def _slope_factor(slope_values_pct: list[float], max_slope_pct: float) -> float:
    """1.0 at 0% grade, falling linearly to 0.0 at max_slope_pct -- the
    same slope ceiling production_area.py already used to decide this is
    a candidate at all, so a patch sitting right at that ceiling (barely
    qualifying) scores near 0 rather than being treated the same as
    dead-flat ground."""
    if not slope_values_pct:
        return 0.0
    avg_slope = float(np.mean(slope_values_pct))
    return max(0.0, min(1.0, 1.0 - avg_slope / max_slope_pct))


def _circular_mean_aspect_deg(aspect_values_deg: list[float]) -> Optional[float]:
    """Mean compass bearing via vector averaging (a plain arithmetic mean
    of e.g. 350 deg and 10 deg would wrongly give 180 instead of 0).
    Returns None if every input is undefined (an all-flat patch)."""
    valid = [a for a in aspect_values_deg if not math.isnan(a)]
    if not valid:
        return None
    sin_sum = sum(math.sin(math.radians(a)) for a in valid)
    cos_sum = sum(math.cos(math.radians(a)) for a in valid)
    return math.degrees(math.atan2(sin_sum, cos_sum)) % 360


def _compactness_score(cells: list[tuple[int, int]], dem: dict) -> float:
    """0-1 shape-compactness score for a patch's own constituent DEM
    cells (NOT its convex-hull footprint -- a convex hull is always
    convex by construction, so it could never register fragmentation).
    Builds the real footprint as a union of per-cell squares (each cell's
    own ground square, sized by the DEM's resolution) and scores it via
    Polsby-Popper (4*pi*area/perimeter^2), normalized against the most
    compact shape an axis-aligned raster footprint can achieve (a solid
    square block, see _SQUARE_COMPACTNESS) so a compact block reads ~1.0
    and a fragmented/elongated sliver of the same area reads well below
    it."""
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


def _size_factor(area_acres: float, cells: list[tuple[int, int]], dem: dict, reference_max_area_acres: float) -> tuple[float, float, float]:
    """Returns (size_factor, area_score, compactness_score) -- the
    sub-scores are returned too so callers/tests can inspect why a patch
    scored the way it did, not just the blended result."""
    area_score = max(0.0, min(1.0, area_acres / reference_max_area_acres))
    compactness = _compactness_score(cells, dem)
    size_factor = SIZE_AREA_SUBWEIGHT * area_score + SIZE_SHAPE_SUBWEIGHT * compactness
    return size_factor, area_score, compactness


def _soil_exclusion_check(soil_component_rows: Optional[list[dict]]) -> tuple[bool, Optional[str], bool]:
    """Returns (soil_exclusion_passed, soil_exclusion_reason, data_available).

    soil_component_rows is get_soil_data_for_polygon()'s own output shape
    (one row per soil component intersecting the patch's footprint,
    carrying hydricrating + drainagecl among other fields), or None (fetch
    failed/wasn't attempted -- distinct from [] which means "fetched fine,
    nothing intersects this footprint," same None-vs-[] convention
    solar_suitability.py's road_geometries_utm already uses).

    Checks EVERY component (not just the dominant one per map unit) --
    unlike a graded score, where averaging in a minor component's value
    would be misleading, a single genuinely hydric/waterlogged inclusion
    within an otherwise-fine map unit is still real, disqualifying ground
    worth flagging conservatively, not diluted away by more-dominant
    components.

    If no SSURGO data was available at all, this can't be verified --
    defaults to PASSED (not excluded) rather than assuming a disqualifying
    condition that was never actually checked; data_available=False lets
    the caller state that plainly in confidence_notes instead of silently
    treating "unknown" as "clean.\""""
    if not soil_component_rows:
        return True, None, False

    for row in soil_component_rows:
        reason = is_disqualifying_soil_condition(row.get("hydricrating"), row.get("drainagecl"))
        if reason:
            return False, reason, True

    return True, None, True


def score_production_areas(
    patches: list[dict],
    dem: dict,
    soil_components_by_patch_id: Optional[dict[int, Optional[list[dict]]]] = None,
    max_slope_pct: float = MAX_PRODUCTION_SLOPE_PCT,
    reference_max_area_acres: float = REFERENCE_MAX_AREA_ACRES,
) -> list[dict]:
    """
    Pure scoring logic -- see module docstring for why this takes
    already-computed inputs rather than fetching anything.

    patches is identify_production_areas()'s own output (production_area.py,
    UNCHANGED -- this function does not alter membership, geometry, or
    which patches exist, only adds score/exclusion fields to each).

    soil_components_by_patch_id maps patch['id'] to that patch's own
    pre-fetched get_soil_data_for_polygon() rows (or None -- see
    _soil_exclusion_check()'s docstring for the None-vs-[] convention);
    omit entirely (None) to run every patch with the soil exclusion check
    defaulted to passed/unavailable.

    max_slope_pct MUST match whatever max_slope_pct produced `patches` (the
    default already matches identify_production_areas()'s own default) --
    see the cell-recovery note below for why a mismatch would silently
    recompute the wrong component labeling.

    Recovers each patch's own constituent DEM cells (not just its
    convex-hull footprint) by recomputing the exact same low-slope mask +
    8-connected-component labeling production_area.py's own
    identify_production_areas() used internally (same compute_slope_percent()
    and MAX_PRODUCTION_SLOPE_PCT it already exposes, same
    raster_grid.connected_components()) -- deterministic, so patch['id']
    lines up with the recomputed component label exactly. This is reusing
    production_area.py's own building blocks, not reimplementing or
    changing its detection logic.

    Returns patches (list of dicts, same objects extended in place; also
    returned for convenience) each with these fields added:
        {
            'suitability_score': float,          # 0-100, slope/size/aspect only
            'slope_factor': float,                # 0-1
            'size_factor': float,                 # 0-1
            'aspect_factor': float,               # 0-1
            'avg_slope_pct': float,
            'aspect_deg': Optional[float],
            'area_score': float,                  # 0-1, size_factor's area sub-score
            'compactness_score': float,           # 0-1, size_factor's shape sub-score
            'soil_exclusion_passed': bool,         # False = genuinely disqualifying soil found
            'soil_exclusion_reason': Optional[str],# populated only when soil_exclusion_passed is False
            'soil_data_available': bool,
            'aspect_available': bool,
            'rank': Optional[int],                # 1 = highest suitability_score among PASSING
                                                    # patches; None for excluded patches (not ranked)
        }

    Excluded patches (soil_exclusion_passed is False) are NOT dropped from
    the returned list -- production_area.py already identified them as
    candidates and this pass doesn't remove candidates, only scores/flags
    them -- but they're left out of rank (get rank=None) and sorted after
    every passing patch, so a disqualified zone can't read as "top-ranked"
    just because its topography happens to score well. This is the
    concrete effect the soil exclusion check has -- unlike a merely
    mediocre-but-workable soil, which has NO effect on score or rank.
    """
    soil_components_by_patch_id = soil_components_by_patch_id or {}

    array = dem["array"]
    resolution = dem["resolution_meters"]

    slope = compute_slope_percent(array, resolution)
    _, aspect_deg = compute_slope_and_aspect(array, resolution)

    candidate_mask = (~np.isnan(slope)) & (slope <= max_slope_pct)
    labels, _ = connected_components(candidate_mask)

    for patch in patches:
        cells = [(int(r), int(c)) for r, c in np.argwhere(labels == patch["id"])]

        slope_values = [float(slope[r, c]) for r, c in cells]
        slope_factor = _slope_factor(slope_values, max_slope_pct)

        aspect_values = [float(aspect_deg[r, c]) for r, c in cells if not math.isnan(aspect_deg[r, c])]
        mean_aspect = _circular_mean_aspect_deg(aspect_values)
        aspect_available = mean_aspect is not None
        aspect_factor = aspect_score(mean_aspect) if aspect_available else 1.0

        size_factor, area_score, compactness_score = _size_factor(
            patch["area_acres"], cells, dem, reference_max_area_acres
        )

        soil_exclusion_passed, soil_exclusion_reason, soil_data_available = _soil_exclusion_check(
            soil_components_by_patch_id.get(patch["id"])
        )

        composite = (
            SLOPE_FACTOR_WEIGHT * slope_factor
            + SIZE_FACTOR_WEIGHT * size_factor
            + ASPECT_FACTOR_WEIGHT * aspect_factor
        )

        patch.update(
            {
                "suitability_score": round(composite * SUITABILITY_SCORE_SCALE, 1),
                "slope_factor": round(slope_factor, 3),
                "size_factor": round(size_factor, 3),
                "aspect_factor": round(aspect_factor, 3),
                "avg_slope_pct": round(float(np.mean(slope_values)), 1) if slope_values else None,
                "aspect_deg": round(mean_aspect, 1) if mean_aspect is not None else None,
                "area_score": round(area_score, 3),
                "compactness_score": round(compactness_score, 3),
                "soil_exclusion_passed": soil_exclusion_passed,
                "soil_exclusion_reason": soil_exclusion_reason,
                "soil_data_available": soil_data_available,
                "aspect_available": aspect_available,
            }
        )

    passing = [p for p in patches if p["soil_exclusion_passed"]]
    excluded = [p for p in patches if not p["soil_exclusion_passed"]]

    passing.sort(key=lambda p: -p["suitability_score"])
    for rank, patch in enumerate(passing, start=1):
        patch["rank"] = rank
    for patch in excluded:
        patch["rank"] = None

    patches[:] = passing + excluded
    return patches


def production_suitability_to_geojson(scored_patches: list[dict]) -> dict:
    """Wraps score_production_areas() output as a schema-conformant
    GeoJSON FeatureCollection on the SAME layer production_area.py's own
    production_areas_to_geojson() uses ("production_area_candidate") --
    these are the same zones, just enriched with suitability_score, its
    component factors, and the soil exclusion check -- not a new/
    different set of candidates."""
    features = []
    for patch in scored_patches:
        confidence_notes = PRODUCTION_SUITABILITY_CONFIDENCE_NOTES_TEMPLATE.format(
            slope_weight=SLOPE_FACTOR_WEIGHT,
            size_weight=SIZE_FACTOR_WEIGHT,
            aspect_weight=ASPECT_FACTOR_WEIGHT,
            soil_availability=_SOIL_AVAILABLE_NOTE if patch["soil_data_available"] else _SOIL_ESTIMATED_NOTE,
            aspect_availability=_ASPECT_AVAILABLE_NOTE if patch["aspect_available"] else _ASPECT_OMITTED_NOTE,
        )
        if patch["soil_exclusion_passed"]:
            label = f"Production area candidate {patch['id']} (suitability rank {patch['rank']})"
        else:
            label = f"Production area candidate {patch['id']} (EXCLUDED -- {patch['soil_exclusion_reason']})"

        features.append(
            make_feature(
                feature_id=f"production-area-{patch['id']}",
                geometry=patch["geometry_wgs84"],
                layer="production_area_candidate",
                label=label,
                confidence=CONFIDENCE_LOW,
                confidence_notes=confidence_notes,
                extra_properties={
                    "area_acres": patch["area_acres"],
                    "representative_elevation_m": round(patch["representative_elevation_m"], 1),
                    "rank": patch["rank"],
                    "suitability_score": patch["suitability_score"],
                    "slope_factor": patch["slope_factor"],
                    "size_factor": patch["size_factor"],
                    "aspect_factor": patch["aspect_factor"],
                    "avg_slope_pct": patch["avg_slope_pct"],
                    "aspect_deg": patch["aspect_deg"],
                    "soil_exclusion_passed": patch["soil_exclusion_passed"],
                    "soil_exclusion_reason": patch["soil_exclusion_reason"],
                    "soil_data_available": patch["soil_data_available"],
                },
            )
        )
    return make_feature_collection(features)


def summarize_production_area_suitability(scored_patches: list[dict]) -> str:
    if not scored_patches:
        return "No production-area candidates to score."

    passing = sorted((p for p in scored_patches if p["soil_exclusion_passed"]), key=lambda p: p["rank"])
    excluded = [p for p in scored_patches if not p["soil_exclusion_passed"]]

    lines = [f"Production-area suitability ranking ({len(passing)} ranked, {len(excluded)} excluded):"]
    for patch in passing:
        lines.append(
            f"  - Rank {patch['rank']}: patch {patch['id']}, score {patch['suitability_score']}/100 "
            f"(slope={patch['slope_factor']}, size={patch['size_factor']}, aspect={patch['aspect_factor']}), "
            f"{patch['area_acres']} acres"
        )
    for patch in excluded:
        lines.append(
            f"  - EXCLUDED: patch {patch['id']}, {patch['area_acres']} acres -- {patch['soil_exclusion_reason']}"
        )
    return "\n".join(lines)


def identify_production_area_suitability(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    check_soil: bool = True,
    **score_kwargs,
) -> dict:
    """
    Full pipeline entry point: fetches the DEM (unless one is passed in),
    identifies production-area candidates (production_area.py, unchanged),
    fetches SSURGO soil component data per candidate footprint for the
    exclusion check, scores them, and returns the enriched
    "production_area_candidate" GeoJSON FeatureCollection.

    Each candidate's own SSURGO fetch degrades independently and
    gracefully (a USDA SDA outage for one candidate's footprint shouldn't
    block scoring for the others, or block the whole pass) -- same
    reasoning solar_suitability.py's prime-farmland check and every other
    optional network layer in this pipeline already uses.
    """
    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    patches = identify_production_areas(dem)

    soil_components_by_patch_id: dict[int, Optional[list[dict]]] = {}

    if check_soil:
        for patch in patches:
            wkt_polygon = coordinates_to_wkt_polygon(patch["geometry_wgs84"]["coordinates"][0])
            try:
                soil_components_by_patch_id[patch["id"]] = get_soil_data_for_polygon(wkt_polygon)
            except Exception:
                soil_components_by_patch_id[patch["id"]] = None

    scored = score_production_areas(patches, dem, soil_components_by_patch_id, **score_kwargs)

    return {"zones_geojson": production_suitability_to_geojson(scored), "scored_patches": scored}


if __name__ == "__main__":
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]

    print("Scoring production-area candidates for property boundary...\n")

    try:
        result = identify_production_area_suitability(property_boundary)
        print(summarize_production_area_suitability(result["scored_patches"]))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map services and USDA's Soil Data Access — not a fully "
            "sandboxed environment."
        )
