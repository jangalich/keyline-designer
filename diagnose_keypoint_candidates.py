"""
diagnose_keypoint_candidates.py

THE KEYPOINT CANDIDATE TABLES, PER VALLEY, ON THE REFERENCE PROPERTY.

    python3 diagnose_keypoint_candidates.py            # the stored 3DEP fixture, offline
    python3 diagnose_keypoint_candidates.py --live     # a fresh 3DEP fetch, over the network

A read-only instrument over keypoint_detection.detect_keypoints()'s own
diagnostics. It prints what the detector decided and why; it computes no
keypoint quantity of its own. Every candidate figure (split cell,
elevation, slope drop, residual, fill depth, outcome, reasons) is read off
the diagnostics dict the detector filled in.

WHAT IT ANSWERS.
  1. Per valley, EVERY candidate split: cell, elevation, slope drop, fit
     residual, fill depth, distance outside the boundary, and the outcome
     (selected / survived / rejected with each reason).
  2. How often fall-through fired: valleys whose selected keypoint is NOT
     the global best-residual split, and the subset where the thing that
     moved it was the fill-artifact gate (the case this branch changed).
  3. Rejection reasons tallied across the run, so fill-artifact
     contamination is a count rather than an inference.
  4. Which valleys REGAINED a keypoint -- the retired single-split search
     would have handed them to the fill gate and returned nothing -- and,
     for each, what rejected the previous best.
  5. RIM ADJACENCY. A selected split whose immediate stem neighbour was
     rejected as a fill artifact sits on the edge of filled ground. On a
     synthetic bowl (test_keypoint_detection.py, test 1) that is exactly
     where fall-through lands: the residual surface itself is shaped by the
     depression, so the best UNFILLED split is the rim. Counted here so a
     real run shows whether it happens, not whether it could.
  6. ELEVATION, FOR THE RECORD ONLY. Each selected keypoint's and each
     rejected candidate's elevation against the parcel's on-parcel raw
     elevation range. Nothing acts on this; it is the evidence for whether
     low keypoint placement is terrain or contamination.

The ONE figure computed here rather than read is the on-parcel elevation
range in 6: the detector has no reason to know it, and it is a plain
min/max over raw DEM cells whose centres lie inside the drawn boundary.
"""

import sys

import numpy as np
from shapely.geometry import Point

import keypoint_detection as kd
import valley_delineation
from raster_grid import pixel_center_xy
from reference_fixture import BOUNDARY_POLYGON_UTM, REAL_BOUNDARY

LIVE = "--live" in sys.argv


def _load_dem() -> tuple[dict, str]:
    if LIVE:
        import dem_data

        return dem_data.get_dem_for_boundary(REAL_BOUNDARY), "live 3DEP fetch (dem_data.get_dem_for_boundary)"
    import terrain_reference_fixture

    return terrain_reference_fixture.load_dem(), (
        f"stored fixture terrain_reference_fixture.npz (retrieved "
        f"{terrain_reference_fixture.load_record()['retrieved_on']})"
    )


def _on_parcel_elevations(dem: dict) -> np.ndarray:
    array = dem["array"]
    rows, cols = array.shape
    values = [
        float(array[r, c])
        for r in range(rows)
        for c in range(cols)
        if BOUNDARY_POLYGON_UTM.contains(Point(*pixel_center_xy(dem, r, c)))
    ]
    return np.asarray(values, dtype=float)


def _position(elevation: float, lo: float, hi: float, on_parcel: np.ndarray) -> str:
    """Where an elevation sits in the parcel's range: fraction of the
    min-max span, and the percentile rank among on-parcel cells."""
    span = (elevation - lo) / (hi - lo) if hi > lo else float("nan")
    rank = float(np.mean(on_parcel <= elevation)) * 100.0
    return f"{span * 100.0:5.1f}% of span, p{rank:5.1f}"


def _fill_adjacent(record: dict, index: int) -> bool:
    by_index = {cand["index"]: cand for cand in record["candidates"]}
    return any(
        kd.REJECT_FILL_ARTIFACT in by_index[i]["rejected_by"]
        for i in (index - 1, index + 1)
        if i in by_index
    )


def main() -> int:
    dem, source = _load_dem()
    valleys = valley_delineation.delineate_valleys(dem)
    diagnostics: dict = {}
    keypoints = kd.detect_keypoints(dem, BOUNDARY_POLYGON_UTM, valleys=valleys, diagnostics=diagnostics)
    records = diagnostics["valley_candidates"]

    on_parcel = _on_parcel_elevations(dem)
    lo, hi = float(on_parcel.min()), float(on_parcel.max())

    print(f"DEM: {source}; {dem['array'].shape[0]}x{dem['array'].shape[1]} cells")
    print(f"primary valleys {diagnostics['valleys']} {[int(v['id']) for v in valleys]}; "
          f"keypoints {len(keypoints)} (pre-epsilon record: 5; retired single-split search on this main: 3)")
    print(f"valley-level outcomes: short_stem={diagnostics['rejected_short_stem']} "
          f"valley_off_margin={diagnostics['rejected_valley_off_margin']} "
          f"no_slope_drop={diagnostics['rejected_no_slope_drop']} "
          f"off_margin={diagnostics['rejected_off_margin']} "
          f"fill_artifact={diagnostics['rejected_fill_artifact']} surviving={diagnostics['surviving']}")
    print(f"on-parcel raw elevation: {lo:.2f} - {hi:.2f} m ({len(on_parcel)} cells)")
    profiled = {r["valley_id"] for r in records}
    unprofiled = [int(v["id"]) for v in valleys if int(v["id"]) not in profiled]
    if unprofiled:
        print(f"valleys never profiled (short stem, or no stem cell within "
              f"{kd.KEYPOINT_BOUNDARY_MARGIN_METERS:.0f} m of the boundary): {unprofiled}")

    # 1. Per-valley candidate tables.
    for record in records:
        print()
        print(f"=== valley {record['valley_id']}: stem {record['stem_length_cells']} cells, "
              f"{len(record['candidates'])} candidates; outcome {record['outcome']}; "
              f"global argmin idx {record['global_argmin_index']}, "
              f"retired (pre-fill-gate) best idx {record['pre_fill_best_index']}, "
              f"selected idx {record['selected_index']}")
        print(f"  {'idx':>4} {'cell':>10} {'elev m':>8} {'drop %':>7} {'resid':>11} {'fill m':>7} "
              f"{'out m':>6}  outcome")
        for cand in record["candidates"]:
            marks = []
            if cand["index"] == record["global_argmin_index"]:
                marks.append("global-argmin")
            if cand["index"] == record["pre_fill_best_index"]:
                marks.append("retired-best")
            outcome = cand["outcome"] if not cand["rejected_by"] else "rejected: " + ", ".join(cand["rejected_by"])
            print(f"  {cand['index']:>4} {str(cand['rowcol']):>10} {cand['elevation_m']:>8.2f} "
                  f"{cand['slope_drop_pct']:>7.2f} {cand['residual']:>11.3f} {cand['fill_depth_m']:>7.2f} "
                  f"{cand['distance_outside_boundary_m']:>6.1f}  {outcome}"
                  + (f"  [{' '.join(marks)}]" if marks else ""))

    # 2. Fall-through.
    print()
    print(f"fall-through fired (selected != global argmin): {diagnostics['fall_through_valleys']} of "
          f"{sum(1 for r in records if r['outcome'] == 'selected')} valleys with a keypoint")
    for record in records:
        if record["fall_through"]:
            by_index = {cand["index"]: cand for cand in record["candidates"]}
            argmin = by_index[record["global_argmin_index"]]
            print(f"  valley {record['valley_id']}: global argmin idx {argmin['index']} rejected by "
                  f"{argmin['rejected_by']}; selected idx {record['selected_index']}")
    print(f"  of which the fill gate moved it (the new case): {diagnostics['fill_fall_through_valleys']}")

    # 3. Rejection tally.
    print(f"rejection reasons across the run (a candidate counts once per reason): "
          f"{diagnostics['candidate_rejections']}")
    total = sum(len(r["candidates"]) for r in records)
    print(f"  candidates evaluated: {total}")

    # 4. Regained valleys.
    regained = [r for r in records if r["fill_fall_through"]]
    print(f"valleys that REGAINED a keypoint (retired search's best split fill-rejected, a later one kept): "
          f"{[r['valley_id'] for r in regained] or 'none'}")
    for record in regained:
        by_index = {cand["index"]: cand for cand in record["candidates"]}
        previous = by_index[record["pre_fill_best_index"]]
        print(f"  valley {record['valley_id']}: previous best idx {previous['index']} {previous['rowcol']} "
              f"rejected by {previous['rejected_by']} (fill {previous['fill_depth_m']:.2f} m)")

    # 5. Rim adjacency.
    rim = [r["valley_id"] for r in records if r["selected_index"] is not None and _fill_adjacent(r, r["selected_index"])]
    print(f"selected keypoints adjacent to a fill-rejected split (rim of filled ground): {rim or 'none'}")

    # 6. Elevation, for the record only.
    print()
    print(f"ELEVATION (record only -- selection never reads it); parcel range {lo:.2f} - {hi:.2f} m")
    for kp in keypoints:
        where = "on parcel" if kp["on_parcel"] else f"{kp['distance_outside_boundary_m']} m out"
        print(f"  keypoint {kp['id']} valley {kp['valley_id']} {kp['rowcol']}: {kp['elevation_m']:.2f} m, "
              f"{_position(kp['elevation_m'], lo, hi, on_parcel)}, {where}")
    for record in records:
        rejected = [cand for cand in record["candidates"] if cand["rejected_by"]]
        if not rejected:
            print(f"  valley {record['valley_id']}: no rejected candidates")
            continue
        elevations = np.array([cand["elevation_m"] for cand in rejected])
        by_reason = {}
        for cand in rejected:
            for reason in cand["rejected_by"]:
                by_reason.setdefault(reason, []).append(cand["elevation_m"])
        print(f"  valley {record['valley_id']} rejected candidates: {len(rejected)}, elevation "
              f"{elevations.min():.2f} - {elevations.max():.2f} m "
              f"({_position(float(elevations.min()), lo, hi, on_parcel)} to "
              f"{_position(float(elevations.max()), lo, hi, on_parcel)})")
        for reason, values in by_reason.items():
            print(f"    {reason}: {len(values)}, {min(values):.2f} - {max(values):.2f} m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
