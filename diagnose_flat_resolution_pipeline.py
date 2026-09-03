"""
diagnose_flat_resolution_pipeline.py

THE PIPELINE-WIDE BEFORE/AFTER INSTRUMENT for the Garbrecht-Martz flat
resolution branch: one parcel, every KSOP consumer, run twice -- once with
the hydrologic conditioning pinned to the OLD Priority-Flood+epsilon, once
with the flat resolution this branch adds -- and every count, acreage and
rank-1 selection printed side by side.

WHY THIS FILE EXISTS RATHER THAN A LIVE REFERENCE-PROPERTY RUN. The real
six-point reference property needs elevation.nationalmap.gov and
sdmdataaccess.sc.egov.usda.gov, and this sandbox's egress policy blocks
both (a confirmed policy denial, not a transient failure -- the same
blocker the Roadmap already records against
production_suitability.py). So the parcel here is a STAND-IN: the
reference property's real boundary, real UTM grid geometry and real ~346 m
elevation band, over a SYNTHETIC surface built to carry the one thing this
branch acts on and the reference property's own DEM is not known to have
much of -- genuine flats. Read the numbers below as "what flat resolution
does to a parcel that HAS flats", not as "what it does to the reference
property". The reference-property run is still outstanding and is called
out as such in the README.

THE FOUR FLATS, each a different case the algorithm has to decide:
    * a MARSH FLAT -- dead level, fed by higher ground on one side,
      spilling to the incised channel on the other. The fully-determined
      case: both gradients have something to say.
    * a CLOSED BASIN -- a pit that the fill raises to a flat floor whose
      rim sits at the spill elevation. The INLET-LESS case.
    * a GRADED BENCH -- a level terrace cut into the slope, with higher
      ground above it and a drop below along its whole length. The
      MULTI-OUTLET case.
    * a BORDER FLAT -- level ground running off the DEM's own edge, whose
      only exit is the grid rim.

Run:  python diagnose_flat_resolution_pipeline.py     (no network)
"""

import sys
from unittest import mock

import numpy as np
from shapely.geometry import Polygon

import canopy_height_data
import keypoint_detection
import pipeline_context
import production_area
import valley_delineation
import water_candidate_zones
import water_survey_areas
from dem_data import _utm_epsg_for_lonlat
from rasterio.warp import transform as warp_transform

# The reference property's own six-point boundary and grid geometry,
# inlined rather than imported from test_step_commit.py -- importing that
# module would run its whole test body as a side effect.
REAL_BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]
_mean_lon = sum(lon for lon, _ in REAL_BOUNDARY) / len(REAL_BOUNDARY)
_mean_lat = sum(lat for _, lat in REAL_BOUNDARY) / len(REAL_BOUNDARY)
CRS = f"EPSG:{_utm_epsg_for_lonlat(_mean_lon, _mean_lat)}"
_xs, _ys = warp_transform(
    "EPSG:4326", CRS, [lon for lon, _ in REAL_BOUNDARY], [lat for _, lat in REAL_BOUNDARY]
)
BOUNDARY_POLYGON_UTM = Polygon(zip(_xs, _ys))
_minx, _miny, _maxx, _maxy = BOUNDARY_POLYGON_UTM.bounds
RESOLUTION_METERS = 5.0
BUFFER_METERS = 100.0
ORIGIN_X = _minx - BUFFER_METERS
ORIGIN_Y = _maxy + BUFFER_METERS
COLS = int(np.ceil((_maxx - _minx + 2 * BUFFER_METERS) / RESOLUTION_METERS))
ROWS = int(np.ceil((_maxy - _miny + 2 * BUFFER_METERS) / RESOLUTION_METERS))

HYDRIC_COMPONENTS = [
    {"mukey": "111111", "comppct_r": "85", "hydricrating": "Yes", "compname": "Fixture silt loam"}
]
HYDRIC_GEOMETRIES = {
    "111111": {
        "type": "Polygon",
        "coordinates": [[[-79.9830, 40.6434], [-79.9822, 40.6434],
                         [-79.9822, 40.6439], [-79.9830, 40.6439], [-79.9830, 40.6434]]],
    }
}
FIXTURE_ROADS = [
    {"name": "Fixture Rd",
     "geometry": {"type": "LineString",
                  "coordinates": [[-79.9840, 40.6436], [-79.9805, 40.6436]]}}
]

CHANNEL_COL = int(COLS * 0.42)


def build_flat_bearing_dem() -> dict:
    """The reference property's real grid geometry over a synthetic surface
    that carries four genuine flats. Elevations sit in the ~346 m band the
    reference property occupies, at the float32 dtype
    dem_data.get_dem_for_boundary() actually delivers."""
    rows = np.arange(ROWS)[:, None].astype(np.float64)
    cols = np.arange(COLS)[None, :].astype(np.float64)

    # A 4% bench falling to the south, with one incised drainage.
    array = 346.0 + 0.20 * rows + 0.05 * cols
    array -= 9.0 * np.exp(-((cols - CHANNEL_COL) ** 2) / (2 * 3.0**2))

    def level(r0, r1, c0, c1, elevation):
        r1, c1 = min(r1, ROWS), min(c1, COLS)
        array[r0:r1, c0:c1] = elevation

    # 1. MARSH FLAT: fed from the north, spilling west into the channel.
    m_r0, m_r1 = int(ROWS * 0.55), int(ROWS * 0.55) + 14
    m_c0, m_c1 = CHANNEL_COL + 6, CHANNEL_COL + 22
    level(m_r0, m_r1, m_c0, m_c1, float(array[m_r1 - 1, m_c0]))

    # 2. CLOSED BASIN: a pit the fill must raise to a flat floor.
    b_r0, b_r1 = int(ROWS * 0.24), int(ROWS * 0.24) + 12
    b_c0, b_c1 = CHANNEL_COL + 28, CHANNEL_COL + 40
    level(b_r0, b_r1, b_c0, b_c1, float(array[b_r0, b_c0]) - 2.5)

    # 3. GRADED BENCH: a long level terrace with a drop along its length.
    t_r0, t_r1 = int(ROWS * 0.72), int(ROWS * 0.72) + 6
    level(t_r0, t_r1, 4, CHANNEL_COL - 6, float(array[t_r0, 4]))

    # 4. BORDER FLAT: level ground running off the grid's own north edge.
    level(0, 5, CHANNEL_COL + 8, CHANNEL_COL + 30, float(array[4, CHANNEL_COL + 8]))

    return {
        "array": array.astype(np.float32),
        "resolution_meters": (RESOLUTION_METERS, RESOLUTION_METERS),
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
    }


def quantise(dem: dict, step_m: float = 0.5) -> dict:
    """The same parcel with its elevations QUANTISED to a coarse step.

    WHY A SECOND PARCEL. A DEM delivered as a smooth analytic surface has
    only the flats you deliberately put in it, and those flats turn out to
    be the EASY case -- one connected outlet set, which the flood enters
    through, so Priority-Flood+epsilon's rise field already IS a
    breadth-first distance from that outlet and flat resolution has
    nothing to correct. Real elevation rasters are not smooth: they are
    quantised, and quantisation makes many small plateaus with SEVERAL
    spills at DIFFERENT elevations and ragged inlet edges. That is the
    case the two methods genuinely disagree about, because the flood
    enters such a flat through its lowest spill only while flat
    resolution reads every spill as an outlet. Reporting both parcels is
    the honest answer to "does this branch change anything" -- on the
    smooth one it does not, on the quantised one it does."""
    out = dict(dem)
    out["array"] = (np.round(dem["array"].astype(np.float64) / step_m) * step_m).astype(np.float32)
    return out


DEM = build_flat_bearing_dem()
DEM_QUANTISED = quantise(DEM)
# A real canopy grid reading zero height everywhere: this parcel has no
# tree cover. A None here is "no HAG coverage fetched", which
# production_area.get_required_tree_root_zone_mask_utm() refuses outright.
CANOPY = {
    "array": np.zeros((ROWS, COLS), dtype=np.float32),
    "resolution_meters": DEM["resolution_meters"],
    "origin_x": ORIGIN_X,
    "origin_y": ORIGIN_Y,
    "crs": CRS,
    "source_item_id": "fixture-hag-zero",
}
ANCHOR = (
    sum(lon for lon, _ in REAL_BOUNDARY) / len(REAL_BOUNDARY),
    sum(lat for _, lat in REAL_BOUNDARY) / len(REAL_BOUNDARY),
)

# --- the two conditioning variants, and the call counters -------------

_CONSUMERS = (valley_delineation, keypoint_detection, water_candidate_zones, water_survey_areas)


def _epsilon_only(array, *_args, **_kwargs):
    """Merged main's conditioning: Priority-Flood+epsilon, no flat pass."""
    return valley_delineation.fill_depressions(array)


def run(variant: str, dem: dict) -> tuple[pipeline_context.PipelineContext, dict]:
    counts = {"fill_depressions": 0, "resolve_flats": 0, "fill_and_resolve": 0}
    real_fill = valley_delineation.fill_depressions
    real_resolve = valley_delineation.resolve_flats
    real_combined = valley_delineation.fill_and_resolve

    def counted_fill(*a, **k):
        counts["fill_depressions"] += 1
        return real_fill(*a, **k)

    def counted_resolve(*a, **k):
        counts["resolve_flats"] += 1
        return real_resolve(*a, **k)

    def counted_combined(*a, **k):
        counts["fill_and_resolve"] += 1
        return real_combined(*a, **k)

    conditioner = counted_combined if variant == "resolved" else _epsilon_only
    patches = [
        mock.patch.object(valley_delineation, "fill_depressions", counted_fill),
        mock.patch.object(valley_delineation, "resolve_flats", counted_resolve),
        # The network boundaries, closed the same way test_step_commit.py's
        # Harness closes them. No canopy on this parcel: a real None, which
        # every consumer already treats as "no HAG coverage here".
        mock.patch.object(canopy_height_data, "get_canopy_height_for_boundary", return_value=CANOPY),
        mock.patch.object(production_area, "get_canopy_height_for_boundary", return_value=CANOPY),
        mock.patch.object(production_area, "get_soil_data_for_polygon", return_value=HYDRIC_COMPONENTS),
        mock.patch.object(production_area, "get_soil_geometries_for_polygon", return_value=HYDRIC_GEOMETRIES),
    ]
    for module in _CONSUMERS:
        if hasattr(module, "fill_and_resolve"):
            patches.append(mock.patch.object(module, "fill_and_resolve", conditioner))

    for p in patches:
        p.start()
    try:
        context = pipeline_context.build_pipeline_context(
            REAL_BOUNDARY,
            ANCHOR,
            dem=dem,
            boundary_polygon_utm=BOUNDARY_POLYGON_UTM,
            soil_components=HYDRIC_COMPONENTS,
            soil_geometries=HYDRIC_GEOMETRIES,
            farm_roads=FIXTURE_ROADS,
            water_features={"streams": [], "water_bodies": []},
            canopy_height=CANOPY,
            saturated_hydraulic_conductivity=[],
        )
    finally:
        for p in reversed(patches):
            p.stop()
    return context, counts


def _acres(zones, key="area_acres"):
    total = 0.0
    for z in zones:
        props = z.get("properties", z) if isinstance(z, dict) else {}
        total += float(props.get(key) or 0.0)
    return total


def _rank1(zones, score_key, id_key="id"):
    """The top-scoring zone, named and scored. Ties are reported as ties
    rather than silently resolved by list order -- a rank-1 that moved
    because two zones tie is a different finding from one that moved
    because a score moved."""
    if not zones:
        return "-"
    scored = []
    for z in zones:
        props = z.get("properties", z) if isinstance(z, dict) else {}
        scored.append((float(props.get(score_key) or 0.0), str(props.get(id_key, "?"))))
    best = max(scored)[0]
    tied = sorted(name for score, name in scored if score == best)
    label = tied[0] if len(tied) == 1 else f"{tied[0]} (+{len(tied) - 1} tied)"
    return f"{label} @ {best:.4f}"


def summarise(context) -> dict:
    valleys = context.valleys or []
    keypoints = context.keypoints or []
    production = context.production_areas or []
    water = context.water_zones or []
    trees = context.tree_zone_patches or []
    selected_water = context.selected_water_zone
    corridor = context.selected_road_corridor or {}
    solar = context.selected_structure_site or {}
    selected_props = (
        (selected_water.get("properties", selected_water) if selected_water else {}) or {}
    )
    return {
        "valleys (count)": len(valleys),
        "valley branch cells": sum(
            len(b) for v in valleys for b in v.get("branches_rowcol", [])
        ),
        "valley max contributing ac": f"{max((float(v.get('max_contributing_area_acres') or 0.0) for v in valleys), default=0.0):.4f}",
        "keypoints (count)": len(keypoints),
        "keypoint rank-1": (
            f"{keypoints[0].get('valley_id', '?')} @ "
            f"{float(keypoints[0].get('contributing_acres') or 0.0):.4f} ac"
            if keypoints else "-"
        ),
        "production zones (count)": len(production),
        "production acres": f"{_acres(production):.4f}",
        "production rank-1": _rank1(production, "suitability_score"),
        "water survey zones (count)": len(water),
        "water survey acres": f"{_acres(water):.4f}",
        "water rank-1": _rank1(water, "max_suitability", id_key="zone_id"),
        "water selected": f"{selected_props.get('zone_id', selected_props.get('id', '-'))}",
        "water selected max_suit": f"{float(selected_props.get('max_suitability') or 0.0):.4f}",
        "water depression depth m": f"{max((float((z.get('properties', z)).get('depression_depth_max_m') or 0.0) for z in water), default=0.0):.4f}",
        "water TWI score max": f"{max((float((z.get('properties', z)).get('twi_score_max') or 0.0) for z in water), default=0.0):.4f}",
        "water contributing ac": f"{max((float((z.get('properties', z)).get('contributing_area_acres_at_wettest_cell') or 0.0) for z in water), default=0.0):.4f}",
        "road corridor length_m": f"{float(corridor.get('total_length_meters') or 0.0):.2f}",
        "road corridor served ac": f"{float(corridor.get('total_served_acres') or 0.0):.4f}",
        "road corridor max grade %": f"{float(corridor.get('max_grade_pct') or 0.0):.2f}",
        "solar rank-1 score": (
            f"{float(solar.get('suitability_score') or 0.0):.4f}" if solar else "-"
        ),
        "solar footprint ac": (
            f"{float(solar.get('footprint_area_acres') or 0.0):.4f}" if solar else "-"
        ),
        "solar dist to water m": (
            f"{float(solar.get('distance_to_water_zone_m') or 0.0):.2f}" if solar else "-"
        ),
        "tree zones (count)": len(trees),
        "tree zone acres": f"{_acres(trees):.4f}",
        "tree rank-1": _rank1(trees, "tree_suitability_score"),
        "parcel acres": f"{float(context.parcel_acres or 0.0):.4f}",
    }


def report(label: str, dem: dict) -> None:
    print("\n" + "=" * 96)
    print(f"PARCEL: {label}")
    print("=" * 96)
    print(f"DEM {ROWS}x{COLS} @ {RESOLUTION_METERS} m, dtype {dem['array'].dtype}, "
          f"elevation {float(np.nanmin(dem['array'])):.2f}-{float(np.nanmax(dem['array'])):.2f} m")

    plain = valley_delineation.fill_depressions(dem["array"], epsilon_meters=0.0)
    _labels, regions = valley_delineation.find_flat_regions(plain)
    multi = [r for r in regions if len(r["cells"]) > 1]
    multi_outlet = [r for r in multi if len({round(float(plain[c]), 6) for c in r["outlets"]}) > 0
                    and len(r["outlets"]) > 1]
    print(f"Flat regions: {len(multi)} with >1 cell ({sum(len(r['cells']) for r in multi)} cells), "
          f"{len(regions) - len(multi)} single-cell; largest "
          f"{max((len(r['cells']) for r in multi), default=0)} cells; "
          f"{len(multi_outlet)} with more than one outlet cell")

    # The conditioned surface itself, before any consumer sees it.
    eps_surface = valley_delineation.fill_depressions(dem["array"])
    res_surface = valley_delineation.fill_and_resolve(dem["array"])
    e_ftr, e_ftc = valley_delineation.compute_flow_direction(eps_surface, dem["resolution_meters"])
    r_ftr, r_ftc = valley_delineation.compute_flow_direction(res_surface, dem["resolution_meters"])
    rerouted = int(((e_ftr != r_ftr) | (e_ftc != r_ftc)).sum())
    e_acc = valley_delineation.compute_flow_accumulation(eps_surface, e_ftr, e_ftc)
    r_acc = valley_delineation.compute_flow_accumulation(res_surface, r_ftr, r_ftc)
    plain_depth = water_survey_areas.compute_depression_depth(dem["array"], plain)
    res_depth = water_survey_areas.compute_depression_depth(dem["array"], res_surface)
    floor = water_survey_areas.DEPRESSION_NOISE_FLOOR_METERS
    print(
        f"Conditioned surface: {rerouted} of {ROWS * COLS} cells re-routed vs the epsilon; "
        f"accumulation max {int(e_acc.max())} -> {int(r_acc.max())}; "
        f"max rise over the plain fill {float(np.nanmax(res_surface - plain)):.4f} m; "
        f"cells crossing the {floor} m noise floor on the increment alone "
        f"{int((np.nan_to_num(res_surface - plain) >= floor).sum())}; "
        f"cells lifted from 0.0 depression depth into nonzero "
        f"{int(((plain_depth == 0.0) & (res_depth > 0.0)).sum())}"
    )

    before, counts_before = run("epsilon", dem)
    after, counts_after = run("resolved", dem)
    sb, sa = summarise(before), summarise(after)
    width = max(len(k) for k in sb)
    moved_keys = [k for k in sb if sb[k] != sa[k]]
    print(f"\n{'':{width}}   {'epsilon (main)':>22s}   {'flat-resolved':>22s}   moved")
    print("-" * (width + 60))
    for key in sb:
        flag = "  <-- MOVED" if sb[key] != sa[key] else ""
        print(f"{key:{width}}   {str(sb[key]):>22s}   {str(sa[key]):>22s}{flag}")
    print(f"\n{len(moved_keys)} of {len(sb)} reported numbers moved"
          + (f": {', '.join(moved_keys)}" if moved_keys else "."))
    print("Call counts, measured not assumed:")
    print(f"  epsilon variant : {counts_before}")
    print(f"  resolved variant: {counts_after}")


def main():
    print(__doc__.strip().splitlines()[0])
    report("smooth surface, four hand-placed flats", DEM)
    report("the same parcel QUANTISED to 0.5 m (what a real raster looks like)", DEM_QUANTISED)
    return 0


if __name__ == "__main__":
    sys.exit(main())
