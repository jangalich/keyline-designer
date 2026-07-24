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
    size_factor / compactness -- this is the one thing that genuinely
        cannot be computed before clustering exists (a cluster's own shape
        has no meaning before STEP 3 produces it), so it's computed fresh
        here via _compactness_score()/_size_factor(), exactly as before.
    soil_carved_acres/pct -- now simple bookkeeping (STEP 1's
        soil_carved_acres_by_cell/soil_carved_pct_by_cell arrays, looked up
        per cluster via its own cells), not a second carving pass. Hydric
        soil was already excluded at the CELL level, before clustering, so
        there is nothing left to carve or split here -- the old
        continuous-geometry carving/splitting machinery
        (_carve_soil_from_patch(), _fetch_disqualifying_soil_union(), the
        lettered-sub-id splitting) is gone entirely, superseded by STEP 1.

Soil quality is still deliberately NOT a positively-weighted scoring
factor, unlike slope/size/aspect. Per Scale of Permanence sequencing, soil
is step 8 — the LAST step, and treated as the most IMPROVABLE factor of
the sequence. Real hydric/wetland soil still has an ABSOLUTE effect (it's
a hard exclusion, at STEP 1, cell-by-cell) — it's graded soil quality that
deliberately doesn't factor into ranking here.
"""

import math

import numpy as np
from shapely.geometry import box
from shapely.ops import unary_union

from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from raster_grid import pixel_center_xy

# --- composite weights (must sum to 1.0). CONFIGURABLE -- tune against a
# real property once ground-truthed. Soil is deliberately NOT one of these
# -- see module docstring for why it's a hard STEP 1 exclusion instead.
# Aspect is deliberately the smallest weight -- general production
# suitability cares far less about compass orientation than solar siting
# did.
SLOPE_FACTOR_WEIGHT = 0.55
SIZE_FACTOR_WEIGHT = 0.30
ASPECT_FACTOR_WEIGHT = 0.15

_WEIGHT_SUM = SLOPE_FACTOR_WEIGHT + SIZE_FACTOR_WEIGHT + ASPECT_FACTOR_WEIGHT
assert math.isclose(_WEIGHT_SUM, 1.0, abs_tol=1e-6), f"suitability factor weights must sum to 1.0, got {_WEIGHT_SUM}"

# suitability_score is reported on a 0-100 scale (composite of the 0-1
# factors below, rounded to 1 decimal).
SUITABILITY_SCORE_SCALE = 100

# size_factor sub-weights (must sum to 1.0). CONFIGURABLE.
SIZE_AREA_SUBWEIGHT = 0.5
SIZE_SHAPE_SUBWEIGHT = 0.5

# Acreage at/above which the area component of size_factor maxes out at
# 1.0 -- bigger than this doesn't add further score, it's already a large,
# workable block. CONFIGURABLE.
REFERENCE_MAX_AREA_ACRES = 10.0

# Polsby-Popper compactness (4*pi*area/perimeter^2) of a perfect square is
# exactly pi/4 (~0.785), not 1.0 -- and for an axis-aligned raster
# footprint a solid square block of cells is the most compact shape
# achievable. Dividing by this ceiling rescales so a best-case compact
# block reads as 1.0.
_SQUARE_COMPACTNESS = math.pi / 4

PRODUCTION_SUITABILITY_CONFIDENCE_NOTES_TEMPLATE = (
    "This ADDS a suitability ranking to production-zone clusters that survived STEP 1's slope + "
    "hydric-soil cell-level gates and STEP 3's area survival check -- it does not change which ground "
    "counts as a candidate or its boundary. suitability_score (0-100) is a weighted composite of THREE "
    "independently-stored 0-1 factors: slope_factor (weight {slope_weight}, averaged from real per-cell "
    "DEM slope scores already computed before clustering), size_factor (weight {size_weight}, real "
    "geometry: acreage + Polsby-Popper compactness of the cluster's own cell footprint -- a large "
    "irregular sliver scores lower than a compact block of the same acreage), and aspect_factor (weight "
    "{aspect_weight}, deliberately the smallest weight -- general production suitability cares far less "
    "about compass orientation than solar siting did -- {aspect_availability}). Soil quality is "
    "DELIBERATELY NOT one of these weighted factors -- per Scale of Permanence sequencing, soil is step "
    "8, the last and most improvable step. Real hydric/wetland soil already had an ABSOLUTE effect: it "
    "was excluded cell-by-cell BEFORE this cluster was ever formed (see soil_carved_acres/pct for how "
    "much of this cluster's own originally slope-eligible source ground was excluded that way). This is "
    "a topographic + soil-based candidate zone, not a certainty -- ground-truth before committing to it. "
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
        size_weight=SIZE_FACTOR_WEIGHT,
        aspect_weight=ASPECT_FACTOR_WEIGHT,
        aspect_availability=_ASPECT_AVAILABLE_NOTE if patch["aspect_available"] else _ASPECT_OMITTED_NOTE,
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


def _compactness_score(cells: list[tuple[int, int]], dem: dict) -> float:
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


def _size_factor(area_acres: float, cells: list[tuple[int, int]], dem: dict, reference_max_area_acres: float) -> tuple[float, float, float]:
    """Returns (size_factor, area_score, compactness_score) -- the
    sub-scores are returned too so callers/tests can inspect why a cluster
    scored the way it did, not just the blended result."""
    area_score = max(0.0, min(1.0, area_acres / reference_max_area_acres))
    compactness = _compactness_score(cells, dem)
    size_factor = SIZE_AREA_SUBWEIGHT * area_score + SIZE_SHAPE_SUBWEIGHT * compactness
    return size_factor, area_score, compactness


def score_production_areas(
    patches: list[dict],
    dem: dict,
    step1: dict,
    reference_max_area_acres: float = REFERENCE_MAX_AREA_ACRES,
) -> list[dict]:
    """
    STEP 4: advisory-only description of survivors -- see module docstring.

    patches is production_area.cluster_and_gate()'s own output (each entry
    must carry 'cells' and 'source_patch_id' -- UNCHANGED, this function
    does not alter membership, geometry, or which clusters exist; it only
    adds descriptive/ranking fields).

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
                                            # gates on this; slope/size/aspect
            'slope_factor': float,        # 0-1
            'size_factor': float,         # 0-1
            'aspect_factor': float,       # 0-1
            'avg_slope_pct': float,
            'aspect_deg': Optional[float],
            'area_score': float,          # 0-1, size_factor's area sub-score
            'compactness_score': float,   # 0-1, size_factor's shape sub-score
            'aspect_available': bool,
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

    for patch in patches:
        cells = patch["cells"]

        slope_factor = float(np.mean([per_cell_slope_factor[r, c] for r, c in cells]))
        aspect_factor = float(np.mean([per_cell_aspect_factor[r, c] for r, c in cells]))

        avg_slope_pct = float(np.mean([slope_pct[r, c] for r, c in cells]))
        cell_aspects = [float(aspect_deg[r, c]) for r, c in cells]
        mean_aspect = _circular_mean_aspect_deg(cell_aspects)
        aspect_available = mean_aspect is not None

        size_factor, area_score, compactness_score = _size_factor(
            patch["area_acres"], cells, dem, reference_max_area_acres
        )

        composite = (
            SLOPE_FACTOR_WEIGHT * slope_factor
            + SIZE_FACTOR_WEIGHT * size_factor
            + ASPECT_FACTOR_WEIGHT * aspect_factor
        )

        first_r, first_c = cells[0]
        soil_carved_acres = soil_carved_acres_by_cell[first_r, first_c]
        soil_carved_pct = soil_carved_pct_by_cell[first_r, first_c]

        patch.update(
            {
                "suitability_score": round(composite * SUITABILITY_SCORE_SCALE, 1),
                "slope_factor": round(slope_factor, 3),
                "size_factor": round(size_factor, 3),
                "aspect_factor": round(aspect_factor, 3),
                "avg_slope_pct": round(avg_slope_pct, 1),
                "aspect_deg": round(mean_aspect, 1) if mean_aspect is not None else None,
                "area_score": round(area_score, 3),
                "compactness_score": round(compactness_score, 3),
                "aspect_available": aspect_available,
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
    production_areas_to_geojson() uses ("production_area_candidate")."""
    features = []
    for patch in scored_patches:
        confidence_notes = patch["confidence_notes"]

        label = f"Production area candidate {patch['id']} (suitability rank {patch['rank']})"

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
                    "soil_carved_acres": patch["soil_carved_acres"],
                    "soil_carved_pct": patch["soil_carved_pct"],
                    "soil_data_available": patch["soil_data_available"],
                    "source_patch_id": patch["source_patch_id"],
                },
            )
        )
    return make_feature_collection(features)


def summarize_production_area_suitability(scored_patches: list[dict]) -> str:
    if not scored_patches:
        return "No production-area candidates to score."

    lines = [f"Production-area suitability ranking ({len(scored_patches)} candidate(s)):"]
    for patch in sorted(scored_patches, key=lambda p: p["rank"]):
        carved_note = f", {patch['soil_carved_acres']} ac soil-carved from patch {patch['source_patch_id']}" if patch["soil_carved_acres"] > 0 else ""
        lines.append(
            f"  - Rank {patch['rank']}: patch {patch['id']}, score {patch['suitability_score']}/100 "
            f"(slope={patch['slope_factor']}, size={patch['size_factor']}, aspect={patch['aspect_factor']}), "
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
