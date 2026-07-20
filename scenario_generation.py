"""
scenario_generation.py

Replaces the flat "one set of downstream layers against the union of
every production zone candidate" computation with N ranked, complete,
internally-consistent SCENARIOS -- each with its own production-zone
subset and its own water/solar/road/fencing layers computed only against
that subset.

The problem this fixes: computing water/solar/road/fencing candidates
against the union of EVERY production-zone candidate over-excludes.
Zones the user won't actually use (e.g. a low-suitability sliver) still
block water/solar/road candidates from ever being evaluated on that
ground, even in a scenario where that zone wouldn't be picked at all.
This module runs the SAME existing per-layer logic once per candidate
subset instead of once globally, so each scenario's downstream layers
only exclude the ground that scenario itself would actually claim.

    production zone candidates, suitability-scored (production_suitability.py,
    already in main, UNCHANGED here)
        --> [Step 1] bounded subset enumeration (enumerate_production_zone_subsets())
        --> [Step 2] existing downstream logic, re-run per subset:
                water_candidate_zones.find_candidate_zones()      (UNCHANGED)
                road_corridors.find_candidate_road_corridors()    (UNCHANGED)
                solar_suitability.find_candidate_solar_zones()    (UNCHANGED)
                fencing.identify_fencing()                        (UNCHANGED --
                    doesn't consume production zones at all, so it's
                    computed once and reused verbatim per scenario; see
                    its own module docstring)
        --> [Step 3] pairwise downstream-diversity scoring + dedup
                (_is_meaningfully_different())
        --> [Step 4] select + label the top N most-diverse scenarios
                (_select_top_n_scenarios(), _label_and_rationale())

This module does NOT introduce a new siting algorithm anywhere -- every
per-layer computation above is imported and called exactly as it already
exists elsewhere in this codebase. The only things that change per
scenario are (a) WHICH production-zone candidates are passed in as the
exclusion/context geometry, and (b) how the resulting FeatureCollections
are grouped and labeled. production_area.py's candidate detection and
production_suitability.py's scoring are untouched inputs, not modified by
this pass.

Report-narrative wiring (Claude narrating per-scenario tradeoffs in the
Scale of Permanence report) is explicitly OUT of scope here -- same
"self-contained, standalone pass, not yet wired into generate_full_report.py/
report_generator.py" framing production_suitability.py already used for
itself. This module is the next layer up from that one; the report
prompt is a separate, later pass still.

STEP 1 -- bounded subset enumeration (enumerate_production_zone_subsets()):
at or below MAX_ZONES_FOR_FULL_ENUMERATION candidates, every non-empty
subset is enumerated directly (2**n - 1, capped at 31 at the default n=5).
Above that, a ranked heuristic takes over: the top-K candidates BY
suitability_score for every K from 1 to n -- linear in n rather than
combinatorial, and (per the feature spec's own framing) covers a single
best zone, the full union, AND every multi-zone combination in between,
without a combinatorial blowup.

STEP 3 -- "meaningfully different" is a concrete, documented threshold
test (_is_meaningfully_different()), not an implicit judgment call: two
subsets are different if the top-ranked water-system candidate zone's
location moves, the top-ranked solar candidate's location moves, or the
acreage available for other infrastructure shifts by enough to clear both
an absolute and a relative bar. See the threshold constants below for the
exact numbers. Water-system candidate zones (unlike solar/road) have no
suitability_score/rank of their own (water_candidate_zones.py doesn't
score them) -- "top" for that layer is defined here as the largest-area
zone, the most defensible stand-in for "most significant zone" available
from that layer's own output.
"""

from typing import Optional

from rasterio.warp import transform as warp_transform
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from dem_data import get_dem_for_boundary
from farm_roads_data import get_farm_roads_for_boundary
from feature_schema import make_feature_collection
from fencing import identify_fencing
from production_suitability import identify_production_area_suitability
from raster_grid import SQUARE_METERS_PER_ACRE
from road_corridors import (
    _fetch_erosion_prone_union,
    _fetch_existing_road_union,
    _fetch_floodplain_hydric_union,
    corridors_to_geojson,
    find_candidate_road_corridors,
)
from soil_data import coordinates_to_wkt_polygon, get_farmland_classification_for_polygon
from solar_suitability import candidates_to_geojson as solar_candidates_to_geojson
from solar_suitability import find_candidate_solar_zones, flag_prime_farmland_conflicts
from valley_delineation import delineate_valleys
from water_candidate_zones import find_candidate_zones as find_water_zones
from water_candidate_zones import zones_to_geojson as water_zones_to_geojson

# Cost-control cap (Step 1): at or below this many suitability-scored
# production-zone candidates, every non-empty subset is enumerated
# directly (2**n - 1 -- 31 at this default). Above it, enumeration falls
# back to a ranked top-K heuristic over suitability_score instead of full
# brute force. CONFIGURABLE.
MAX_ZONES_FOR_FULL_ENUMERATION = 5

# How many top-ranked, deduplicated scenarios to return (Step 4).
# CONFIGURABLE constant, not hardcoded inline in the selection logic.
DEFAULT_SCENARIO_COUNT = 3

# --- Step 3 "meaningfully different" thresholds -- documented explicitly
# per the feature spec, not left as an implicit judgment call. ---

# Two subsets' top-ranked (largest-area) water-system candidate zone are
# treated as a different LOCATION if their centroids sit farther apart
# than this. CONFIGURABLE.
WATER_TOP_CANDIDATE_CENTROID_THRESHOLD_METERS = 30.0

# Same threshold, for the top-ranked (rank 1) solar candidate.
# CONFIGURABLE.
SOLAR_TOP_CANDIDATE_CENTROID_THRESHOLD_METERS = 30.0

# Acreage available for infrastructure OTHER than production (parcel
# acreage minus a subset's own production-zone union) is "meaningfully
# different" only if it clears BOTH bars below -- an absolute acreage
# difference AND a relative (percentage-of-the-larger-value) difference.
# Requiring both keeps a tiny absolute swing on a huge property, or a
# huge-looking percentage swing on a tiny property, from each being
# misread as "meaningful" on their own. CONFIGURABLE.
AVAILABLE_ACREAGE_ABSOLUTE_THRESHOLD_ACRES = 0.75
AVAILABLE_ACREAGE_RELATIVE_THRESHOLD = 0.10

SCENARIO_ID_PREFIX = "scenario"


def enumerate_production_zone_subsets(
    scored_patches: list[dict],
    max_zones_for_full_enumeration: int = MAX_ZONES_FOR_FULL_ENUMERATION,
) -> list[list[dict]]:
    """
    Step 1: bounded candidate-subset enumeration over already-scored
    production-zone candidates (production_suitability.py's own
    scored_patches -- unchanged, unscored here; this function only
    combines them, never reshapes or rescoring any one of them).

    At or below max_zones_for_full_enumeration candidates: every non-empty
    subset is enumerated directly (2**n - 1) -- cheap enough to brute-force
    at the default cap (31 subsets at n=5), and guarantees every possible
    multi-zone combination is actually considered, not just the extremes.

    Above that: falls back to a ranked heuristic over suitability_score
    instead of full brute force -- the top-K candidates BY SCORE, for
    every K from 1 (the single best zone) through n (the full union), each
    as its own subset. Linear in n rather than exponential, and
    deliberately spans both extremes (a single zone, the full union) AND
    every multi-zone combination in between (top-2, top-3, ...) per the
    feature spec's own "top-1, top-2, top-3 combinations by score" framing
    -- without the combinatorial blowup of considering every arbitrary
    combination once n grows past a handful of candidates.

    Returns each subset as its own list of patch dicts (the original
    objects, unmodified) -- callers identify a subset by the sorted tuple
    of its patches' own 'id' values.
    """
    n = len(scored_patches)
    if n == 0:
        return []

    if n <= max_zones_for_full_enumeration:
        return [
            [scored_patches[i] for i in range(n) if mask & (1 << i)]
            for mask in range(1, 1 << n)
        ]

    ranked = sorted(scored_patches, key=lambda p: -p["suitability_score"])
    return [ranked[:k] for k in range(1, n + 1)]


def _boundary_polygon_utm(boundary_coordinates: list[tuple[float, float]], dem: dict) -> Polygon:
    """Same reprojection every other pipeline-entry-point module in this
    codebase already inlines (production_suitability.py, water_candidate_zones.py,
    solar_suitability.py, road_corridors.py) -- duplicated here rather
    than newly shared, matching this codebase's existing convention of
    each entry point owning its own copy of this one-liner."""
    xs, ys = warp_transform(
        "EPSG:4326",
        dem["crs"],
        [pt[0] for pt in boundary_coordinates],
        [pt[1] for pt in boundary_coordinates],
    )
    return Polygon(zip(xs, ys))


def _fetch_shared_road_geometries_utm(
    boundary_coordinates: list[tuple[float, float]], dem: dict
) -> Optional[list[LineString]]:
    """
    Real existing-road geometry for solar's road-PROXIMITY constraint,
    fetched ONCE and reused across every scenario -- a real road's
    location doesn't depend on which production-zone subset a given
    scenario picked, so there's no reason to re-fetch it per scenario.

    This duplicates the fetch-and-reproject glue
    solar_suitability.identify_solar_candidate_zones() already inlines,
    rather than importing it, because that module keeps this glue
    embedded directly in its own entry-point function instead of
    factoring it out as a standalone helper. find_candidate_solar_zones()
    itself -- the actual constraint-stack/scoring algorithm -- is
    imported and called UNCHANGED below, not reimplemented; only this
    generic "fetch roads, reproject to UTM" glue is duplicated, the same
    way fencing.py already duplicates dem_data.py's _utm_epsg_for_lonlat()
    rather than importing a "private" helper from an unrelated module.

    Returns None if the fetch itself failed (disables the road-proximity
    constraint for every scenario, same as today); otherwise a (possibly
    empty) list of UTM LineStrings.
    """
    try:
        roads = get_farm_roads_for_boundary(boundary_coordinates)
    except Exception:
        return None

    road_geometries_utm = []
    for road in roads:
        geometry = road["geometry"]
        line_lists = geometry["coordinates"] if geometry["type"] == "MultiLineString" else [geometry["coordinates"]]
        for line in line_lists:
            xs, ys = warp_transform("EPSG:4326", dem["crs"], [p[0] for p in line], [p[1] for p in line])
            road_geometries_utm.append(LineString(zip(xs, ys)))
    return road_geometries_utm


def _compute_scenario_layers(
    subset_patches: list[dict],
    dem: dict,
    boundary_polygon_utm: Polygon,
    valleys: list[dict],
    shared_road_geometries_utm: Optional[list[LineString]],
    hydric_floodplain_union,
    floodplain_is_fallback: bool,
    erosion_prone_union,
    erosion_data_unavailable: bool,
    existing_road_union_utm,
    farmland_classifications: Optional[list[dict]],
) -> dict:
    """
    Step 2: re-runs the EXISTING downstream computation against one
    production-zone subset's own union as the exclusion/context geometry.
    water_candidate_zones.find_candidate_zones(),
    road_corridors.find_candidate_road_corridors(), and
    solar_suitability.find_candidate_solar_zones() are called completely
    UNCHANGED -- subset_patches simply stands in for the flat,
    whole-property identify_production_areas() output each of those
    layers' own full pipeline entry point would otherwise compute
    internally. Every other real-data input (DEM, valleys, existing
    roads, floodplain/erosion exclusion) is the same, already-fetched,
    subset-INDEPENDENT geometry shared across every scenario -- only the
    production-zone exclusion differs per call.
    """
    water_zones = find_water_zones(valleys, subset_patches, boundary_polygon_utm, dem["crs"])

    road_candidates = find_candidate_road_corridors(
        dem,
        subset_patches,
        water_zones,
        boundary_polygon_utm,
        hydric_floodplain_union=hydric_floodplain_union,
        erosion_prone_union=erosion_prone_union,
        road_union_utm=existing_road_union_utm,
    )

    if shared_road_geometries_utm:
        solar_road_geometries_utm = shared_road_geometries_utm
        solar_road_is_fallback = False
    elif road_candidates:
        # No real road data reachable at all (matches
        # identify_solar_candidate_zones()'s own "not road_geometries_utm"
        # fallback trigger) -- fall back to THIS scenario's own top-ranked
        # suggested corridor rather than a separately-recomputed global
        # one, so the fallback stays internally consistent with this
        # scenario's own exclusion geometry.
        top_corridor = road_candidates[0]
        solar_road_geometries_utm = [LineString([(p[0], p[1]) for p in top_corridor["points_xyz"]])]
        solar_road_is_fallback = True
    else:
        solar_road_geometries_utm = None
        solar_road_is_fallback = False

    solar_candidates = find_candidate_solar_zones(
        dem, subset_patches, water_zones, solar_road_geometries_utm, boundary_polygon_utm
    )
    if farmland_classifications is not None and solar_candidates:
        solar_candidates = flag_prime_farmland_conflicts(solar_candidates, farmland_classifications)

    return {
        "water_zones": water_zones,
        "road_candidates": road_candidates,
        "solar_candidates": solar_candidates,
        "solar_road_is_fallback": solar_road_is_fallback,
        "floodplain_is_fallback": floodplain_is_fallback,
        "erosion_data_unavailable": erosion_data_unavailable,
    }


def _scenario_diversity_metrics(
    subset_patches: list[dict],
    water_zones: list[dict],
    solar_candidates: list[dict],
    road_candidates: list[dict],
    boundary_polygon_utm: Polygon,
) -> dict:
    """Concrete, comparable numbers for Step 3's diversity test -- see
    _is_meaningfully_different(). 'top' water zone = largest-area zone
    (water_candidate_zones.py doesn't rank/score its own output, unlike
    solar/road); 'top' solar/road candidate = rank 1 (both are already
    sorted best-first by their own find_candidate_*() function)."""
    production_union = unary_union([p["polygon_utm"] for p in subset_patches]) if subset_patches else None
    production_acres = (production_union.area / SQUARE_METERS_PER_ACRE) if production_union is not None else 0.0
    parcel_acres = boundary_polygon_utm.area / SQUARE_METERS_PER_ACRE

    top_water = max(water_zones, key=lambda z: z["polygon_utm"].area) if water_zones else None
    top_solar = solar_candidates[0] if solar_candidates else None

    return {
        "production_acres": round(production_acres, 2),
        "available_acres": round(parcel_acres - production_acres, 2),
        "water_zone_count": len(water_zones),
        "top_water_centroid": top_water["polygon_utm"].centroid if top_water else None,
        "solar_candidate_count": len(solar_candidates),
        "solar_candidate_acres": round(
            sum(c["polygon_utm"].area for c in solar_candidates) / SQUARE_METERS_PER_ACRE, 2
        ),
        "top_solar_centroid": top_solar["polygon_utm"].centroid if top_solar else None,
        "road_candidate_count": len(road_candidates),
    }


def _centroid_distance_or_max(centroid_a, centroid_b, threshold_meters: float) -> float:
    """Distance between two optional shapely Points, with one subset
    having a top candidate on this layer and the other having NONE AT ALL
    treated as maximally different (a flat multiple of the threshold,
    guaranteed to clear it) rather than an undefined/zero distance."""
    if (centroid_a is None) != (centroid_b is None):
        return 3 * threshold_meters
    if centroid_a is None:
        return 0.0
    return centroid_a.distance(centroid_b)


def _centroid_moved(centroid_a, centroid_b, threshold_meters: float) -> bool:
    return _centroid_distance_or_max(centroid_a, centroid_b, threshold_meters) > threshold_meters


def _is_meaningfully_different(metrics_a: dict, metrics_b: dict) -> bool:
    """
    Step 3's concrete diversity test. Two subsets' downstream results are
    treated as meaningfully different -- and therefore NOT duplicates --
    if ANY of the following documented thresholds is cleared:

      1. The top-ranked (largest-area) water-system candidate zone's
         location moves by more than
         WATER_TOP_CANDIDATE_CENTROID_THRESHOLD_METERS (or one subset has
         a top water candidate and the other has none at all).
      2. The top-ranked (rank 1) solar candidate's location moves by more
         than SOLAR_TOP_CANDIDATE_CENTROID_THRESHOLD_METERS (or existence
         differs).
      3. Acreage available for other infrastructure (parcel acreage minus
         THIS subset's own production-zone union) differs by at least
         AVAILABLE_ACREAGE_ABSOLUTE_THRESHOLD_ACRES acres AND at least
         AVAILABLE_ACREAGE_RELATIVE_THRESHOLD of the larger of the two
         values.

    Two subsets clearing none of these are duplicates: their downstream
    results are indistinguishable in every way this pipeline measures, so
    only one is kept as a representative (_select_top_n_scenarios()).
    """
    if _centroid_moved(
        metrics_a["top_water_centroid"], metrics_b["top_water_centroid"], WATER_TOP_CANDIDATE_CENTROID_THRESHOLD_METERS
    ):
        return True
    if _centroid_moved(
        metrics_a["top_solar_centroid"], metrics_b["top_solar_centroid"], SOLAR_TOP_CANDIDATE_CENTROID_THRESHOLD_METERS
    ):
        return True

    acres_a, acres_b = metrics_a["available_acres"], metrics_b["available_acres"]
    absolute_diff = abs(acres_a - acres_b)
    larger = max(acres_a, acres_b, 1e-9)
    if absolute_diff >= AVAILABLE_ACREAGE_ABSOLUTE_THRESHOLD_ACRES and (absolute_diff / larger) >= AVAILABLE_ACREAGE_RELATIVE_THRESHOLD:
        return True

    return False


def _diversity_distance(metrics_a: dict, metrics_b: dict) -> float:
    """A single scalar 'how different' score, each axis normalized by its
    own _is_meaningfully_different() threshold and summed -- used only to
    RANK candidates by diversity once dedup has already established which
    ones aren't duplicates (see _select_top_n_scenarios()); the boolean
    dedup decision itself always goes through _is_meaningfully_different(),
    never this scalar."""
    water_d = _centroid_distance_or_max(
        metrics_a["top_water_centroid"], metrics_b["top_water_centroid"], WATER_TOP_CANDIDATE_CENTROID_THRESHOLD_METERS
    )
    solar_d = _centroid_distance_or_max(
        metrics_a["top_solar_centroid"], metrics_b["top_solar_centroid"], SOLAR_TOP_CANDIDATE_CENTROID_THRESHOLD_METERS
    )
    acreage_d = abs(metrics_a["available_acres"] - metrics_b["available_acres"])
    return (
        water_d / WATER_TOP_CANDIDATE_CENTROID_THRESHOLD_METERS
        + solar_d / SOLAR_TOP_CANDIDATE_CENTROID_THRESHOLD_METERS
        + acreage_d / AVAILABLE_ACREAGE_ABSOLUTE_THRESHOLD_ACRES
    )


def _select_top_n_scenarios(candidates: list[dict], scenario_count: int) -> list[dict]:
    """
    Step 4: dedup, then select the N most-diverse survivors.

    Dedup walks candidates ordered by their own production-zone acreage
    (largest first, so a bigger/more-inclusive subset -- the more
    "obviously distinct" extreme -- wins the representative slot over a
    near-identical smaller one) and keeps a candidate only if it's
    meaningfully different (_is_meaningfully_different()) from EVERY
    representative already kept; otherwise it's a duplicate of one
    already kept, and is dropped.

    If more than scenario_count representatives survive dedup, a greedy
    farthest-point selection picks the scenario_count that are most
    spread out from each other (by _diversity_distance()) rather than an
    arbitrary cut -- starting from the largest-acreage representative
    (the most concrete "Maximum Production" extreme) and repeatedly
    adding whichever remaining representative is farthest (by minimum
    distance to anything already picked) from the picks made so far.
    """
    if not candidates:
        return []

    ordered = sorted(candidates, key=lambda c: -c["metrics"]["production_acres"])

    deduped: list[dict] = []
    for candidate in ordered:
        if all(_is_meaningfully_different(candidate["metrics"], kept["metrics"]) for kept in deduped):
            deduped.append(candidate)

    if len(deduped) <= scenario_count:
        return deduped

    selected = [deduped[0]]
    remaining = deduped[1:]
    while len(selected) < scenario_count and remaining:
        best = max(remaining, key=lambda c: min(_diversity_distance(c["metrics"], s["metrics"]) for s in selected))
        selected.append(best)
        remaining.remove(best)

    return selected


def _label_and_rationale(candidate: dict, selected: list[dict], parcel_acres: float) -> tuple[str, str]:
    """
    Step 4: generates a short label + rationale from concrete
    differentiators (zones included, resulting acreage, downstream
    candidate counts) -- not a generic "Scenario 1/2/3" naming scheme.

    Selected scenarios are ranked by their own production-zone acreage:
    the largest is labeled "Maximum Production" and the smallest
    "Minimum Production Footprint" -- concrete extremes on the actual
    production-vs-other-infrastructure acreage tradeoff this whole
    feature exists to surface. Anything strictly in between is "Balanced".
    """
    by_acres = sorted(selected, key=lambda c: -c["metrics"]["production_acres"])
    position = by_acres.index(candidate)

    if position == 0:
        label = "Maximum Production"
    elif position == len(by_acres) - 1:
        label = "Minimum Production Footprint"
    else:
        label = "Balanced"

    metrics = candidate["metrics"]
    zone_ids = candidate["zone_ids"]
    pct_of_parcel = round(metrics["production_acres"] / parcel_acres * 100, 1) if parcel_acres else 0.0

    rationale = (
        f"Includes {len(zone_ids)} production zone candidate(s) ({', '.join(zone_ids)}), "
        f"totaling {metrics['production_acres']} acres ({pct_of_parcel}% of the parcel) and "
        f"leaving {metrics['available_acres']} acres available for other infrastructure. "
        f"Downstream: {metrics['water_zone_count']} water system candidate zone(s), "
        f"{metrics['solar_candidate_count']} solar candidate(s) ({metrics['solar_candidate_acres']} ac total), "
        f"{metrics['road_candidate_count']} suggested road corridor candidate(s)."
    )
    return label, rationale


def _prefixed_feature_collection(feature_collection: dict, scenario_id: str) -> dict:
    """Wraps an already schema-conformant FeatureCollection with every
    feature's own layer-scoped id prefixed by scenario_id, so ids stay
    globally unique once multiple scenarios' FeatureCollections are
    combined into one response. Purely a wrapping-step rename -- the
    per-layer *_to_geojson() functions themselves, and every other
    property on each feature, are untouched."""
    features = [
        {**feature, "id": f"{scenario_id}__{feature['id']}"} for feature in feature_collection["features"]
    ]
    return make_feature_collection(features)


def generate_production_scenarios(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    scenario_count: int = DEFAULT_SCENARIO_COUNT,
    max_zones_for_full_enumeration: int = MAX_ZONES_FOR_FULL_ENUMERATION,
    check_soil: bool = True,
    check_prime_farmland: bool = True,
) -> list[dict]:
    """
    Full pipeline entry point (Steps 1-4). Fetches the DEM (unless one is
    passed in) and production-zone suitability scores
    (production_suitability.py, UNCHANGED), enumerates bounded candidate
    subsets, re-runs the existing downstream layers once per subset, and
    returns the top scenario_count most-diverse, deduplicated scenarios:

        [
            {
                "scenario_id": "<unique-string>",
                "label": "<short human-readable label>",
                "rationale": "<why this scenario is distinct/included>",
                "production_zones_included": ["<zone-id>", ...],
                "feature_collections": {
                    "water_system_candidates": <FeatureCollection>,
                    "solar_candidates": <FeatureCollection>,
                    "road_corridors": <FeatureCollection>,
                    "exclusion_fencing": <FeatureCollection>,
                },
            },
            ...
        ]

    Returns [] if no production-zone candidates were identified at all --
    there's no exclusion geometry to build scenarios from.

    Every subset-INDEPENDENT real-data input (DEM, valleys, existing
    roads, floodplain/erosion exclusion unions, SSURGO prime-farmland
    classification, and fencing -- which doesn't consume production zones
    at all) is fetched/computed exactly ONCE here and reused across every
    scenario, the same graceful-degradation behavior each of those
    fetches already has in its own module (an outage in any one of them
    doesn't block scenario generation, it just narrows what that
    optional layer can report -- unchanged from today).
    """
    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    boundary_polygon_utm = _boundary_polygon_utm(boundary_coordinates, dem)
    parcel_acres = boundary_polygon_utm.area / SQUARE_METERS_PER_ACRE

    suitability_result = identify_production_area_suitability(boundary_coordinates, dem=dem, check_soil=check_soil)
    scored_patches = suitability_result["scored_patches"]

    if not scored_patches:
        return []

    subsets = enumerate_production_zone_subsets(scored_patches, max_zones_for_full_enumeration)

    # --- subset-INDEPENDENT inputs: fetched/computed exactly once,
    # reused across every scenario below (see docstring above). ---
    valleys = delineate_valleys(dem)
    shared_road_geometries_utm = _fetch_shared_road_geometries_utm(boundary_coordinates, dem)
    hydric_floodplain_union, floodplain_is_fallback = _fetch_floodplain_hydric_union(
        boundary_coordinates, dem, valleys
    )
    erosion_prone_union, erosion_data_unavailable = _fetch_erosion_prone_union(boundary_coordinates, dem)
    existing_road_union_utm = _fetch_existing_road_union(boundary_coordinates, dem)

    farmland_classifications = None
    if check_prime_farmland:
        try:
            wkt_polygon = coordinates_to_wkt_polygon(boundary_coordinates)
            farmland_classifications = get_farmland_classification_for_polygon(wkt_polygon)
        except Exception:
            farmland_classifications = None

    try:
        fencing_result = identify_fencing(boundary_coordinates)
    except Exception:
        # identify_fencing() (unlike every other layer here) doesn't
        # guard its own NHD stream fetch internally -- generate_full_report.py's
        # own Step 9 wraps this exact call in a try/except for the same
        # reason. An outage just means this run's fencing collection is
        # empty (no stream-exclusion features, no perimeter feature),
        # not a reason to fail scenario generation entirely.
        fencing_result = {"fencing_geojson": make_feature_collection([])}

    candidates = []
    for subset_patches in subsets:
        layers = _compute_scenario_layers(
            subset_patches,
            dem,
            boundary_polygon_utm,
            valleys,
            shared_road_geometries_utm,
            hydric_floodplain_union,
            floodplain_is_fallback,
            erosion_prone_union,
            erosion_data_unavailable,
            existing_road_union_utm,
            farmland_classifications,
        )
        metrics = _scenario_diversity_metrics(
            subset_patches,
            layers["water_zones"],
            layers["solar_candidates"],
            layers["road_candidates"],
            boundary_polygon_utm,
        )
        candidates.append(
            {
                "zone_ids": sorted(str(p["id"]) for p in subset_patches),
                "layers": layers,
                "metrics": metrics,
            }
        )

    selected = _select_top_n_scenarios(candidates, scenario_count)

    scenarios = []
    for index, candidate in enumerate(selected, start=1):
        scenario_id = f"{SCENARIO_ID_PREFIX}-{index}"
        label, rationale = _label_and_rationale(candidate, selected, parcel_acres)
        layers = candidate["layers"]

        scenarios.append(
            {
                "scenario_id": scenario_id,
                "label": label,
                "rationale": rationale,
                "production_zones_included": candidate["zone_ids"],
                "feature_collections": {
                    "water_system_candidates": _prefixed_feature_collection(
                        water_zones_to_geojson(layers["water_zones"]), scenario_id
                    ),
                    "solar_candidates": _prefixed_feature_collection(
                        solar_candidates_to_geojson(
                            layers["solar_candidates"], road_data_is_fallback=layers["solar_road_is_fallback"]
                        ),
                        scenario_id,
                    ),
                    "road_corridors": _prefixed_feature_collection(
                        corridors_to_geojson(
                            layers["road_candidates"],
                            floodplain_data_is_fallback=layers["floodplain_is_fallback"],
                            erosion_data_unavailable=layers["erosion_data_unavailable"],
                        ),
                        scenario_id,
                    ),
                    "exclusion_fencing": _prefixed_feature_collection(
                        fencing_result["fencing_geojson"], scenario_id
                    ),
                },
            }
        )

    return scenarios


def summarize_scenarios(scenarios: list[dict]) -> str:
    if not scenarios:
        return "No production-zone candidates identified -- no scenarios generated."

    lines = [f"Generated {len(scenarios)} scenario(s):"]
    for scenario in scenarios:
        lines.append(f"\n{scenario['scenario_id']}: {scenario['label']}")
        lines.append(f"  Zones included: {', '.join(scenario['production_zones_included']) or '(none)'}")
        lines.append(f"  {scenario['rationale']}")
        for key, label in (
            ("water_system_candidates", "Water system candidates"),
            ("solar_candidates", "Solar candidates"),
            ("road_corridors", "Road corridor candidates"),
            ("exclusion_fencing", "Fencing features"),
        ):
            count = len(scenario["feature_collections"][key]["features"])
            lines.append(f"  {label}: {count}")
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

    print("Generating production zone scenarios for property boundary...\n")

    try:
        scenarios = generate_production_scenarios(property_boundary)
        print(summarize_scenarios(scenarios))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map services and USDA's Soil Data Access — not a fully "
            "sandboxed environment."
        )
