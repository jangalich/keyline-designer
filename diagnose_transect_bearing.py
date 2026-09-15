"""
diagnose_transect_bearing.py

IS THE SHALLOW ENCLOSURE A BEARING ARTIFACT? A read-only instrument that
re-measures every surviving compartment's two transects a SECOND way and
prints the two side by side, so one alternative explanation for the
crest-height finding can be ruled in or out before anyone scores,
weights or gates on that field.

THE FINDING THAT PROMPTED IT. The reference run's surviving compartments
report binding-shoulder depths at the pinch of -0.00, 0.05, 0.34 and
0.53 m -- not one dammable cross-section on the property -- and every
compartment reports at least one flank as "no crest within bound", so
the binding minimum is often taken over a SINGLE reading rather than
two. That is either a true statement about the parcel or an artifact of
where the instrument pointed.

THE ALTERNATIVE EXPLANATION, stated precisely.
build_embankment_compartment() takes its transects PERPENDICULAR TO THE
SEED->PINCH BASELINE -- one straight line from the storage anchor to the
dam reach -- not perpendicular to the channel's LOCAL direction at each
station. On a valley that curves between those two points the baseline
is a chord across the bend, and its perpendicular can run obliquely to
the true cross-section. In the limit, where the baseline runs nearly
PERPENDICULAR to the local channel, the transect runs nearly ALONG the
valley floor: walking one way it climbs the channel's own gradient and
never falls (absent), walking the other it falls immediately and
declares a crest at the station itself (0.00 m). Shallow depths and
absent flanks in the same place are exactly that signature, which is
why it has to be excluded before the numbers are believed.

THE TWO METHODS.
    A -- BASELINE-PERPENDICULAR. What production does today, reproduced
         here rather than read off the record, and then CHECKED against
         the record (see the agreement column) so the comparison is
         provably like-for-like.
    B -- LOCAL-CHANNEL-PERPENDICULAR. Perpendicular to the channel's own
         direction at that station, from valley_level_pool.
         local_stem_direction() -- the DE-QUANTIZED SECANT over
         +/- STEM_DIRECTION_WINDOW_CELLS channel cells. Reused, not
         reimplemented: this module defines no direction estimator and
         no crest walk of its own.

Same ridge_crest_walk(), same RIDGE_PROMINENCE_METERS, same
RIDGE_WALK_MAX_HALF_WIDTH_METERS, same crest_height_above_channel()
absent semantics. The ONLY thing that differs between A and B is the
bearing, which is what makes the difference attributable to it.

A NOTE ON THE SECANT'S SOURCE, because it is not the one the design
brief assumed. walk_embankment_pinch() does NOT use a de-quantized
secant: it measures its widths perpendicular to _flow_direction_unit(),
the RAW D8 STEP, which names one of only eight directions and can
therefore sit up to 22.5 degrees off the true channel line. The
de-quantized secant is local_stem_direction() in valley_level_pool.py,
written for the level-pool arc's abutment search for exactly this
reason. This module reuses THAT one, which is the better instrument and
the one the brief describes; the discrepancy is reported rather than
papered over, because "the pinch walk's own widths are measured off a
45-degree-quantized bearing" is itself a finding this comparison
surfaces.

THE STEM AND ITS CLAMPING, disclosed. The channel line fed to the
secant is the pinch walk's own station list (zone['walk_stations']),
which begins AT the seed. local_stem_direction() clamps its window at
the stem's ends, so the seed station's secant is ONE-SIDED (it can only
look downstream) while an interior station's is two-sided. A one-sided
secant over 2 cells is still far better than a single D8 step, but it
is a weaker estimate, and a seed-station disagreement should be read
with that in mind. Extending the stem upstream of the seed would need
an upstream trace this instrument deliberately does not add.

READ-ONLY, AND DIAGNOSTIC-ONLY. Nothing here is imported by any
production path, nothing it computes is stored on a zone, and the
production transect bearing is untouched. If the verdict says BEARING
ARTIFACT, changing that bearing is its own branch; this module's
output IS the deliverable.
"""

import math
from typing import Optional

from raster_grid import pixel_center_xy
from valley_level_pool import (
    STEM_DIRECTION_WINDOW_CELLS,
    bearing_degrees,
    local_stem_direction,
)
from water_survey_areas import (
    RIDGE_PROMINENCE_METERS,
    RIDGE_WALK_MAX_HALF_WIDTH_METERS,
    SURVEY_TYPE_EMBANKMENT,
    crest_height_above_channel,
    lower_crest_height,
    ridge_crest_walk,
)

METHOD_BASELINE = "A/baseline-perp"
METHOD_LOCAL = "B/local-channel-perp"

# ==========================================================================
# THE VERDICT RULE, PINNED BEFORE THE DATA
# ==========================================================================
# Written here, as constants, so the threshold cannot be chosen after
# seeing which way the numbers fell. A station counts as MATERIALLY
# DEEPER under B when B's binding depth beats A's by more than the same
# prominence the crest walk itself runs on -- 1.0 m. Using
# RIDGE_PROMINENCE_METERS rather than a new number is deliberate: a
# difference smaller than the threshold that DECLARES a crest is not
# evidence that the crest was in the wrong place.
VERDICT_MARGIN_METERS = RIDGE_PROMINENCE_METERS

# "A majority of stations" -- strictly more than half of the stations
# compared, counting every surviving compartment's seed and pinch.
VERDICT_MAJORITY_FRACTION = 0.5

VERDICT_BEARING_ARTIFACT = "BEARING ARTIFACT"
VERDICT_TERRAIN = "TERRAIN"
VERDICT_MIXED = "MIXED"

# Per-station classifications feeding the verdict.
DEEPER_UNDER_B = "deeper_under_B"
DEEPER_UNDER_A = "deeper_under_A"
COMPARABLE = "comparable"
NEITHER_MEASURED = "neither_measured"


def _angle_between_degrees(first: tuple, second: tuple) -> float:
    """The angle between two unit vectors, in [0, 180] degrees.

    This is also the angle between their PERPENDICULARS: rotating both
    by 90 degrees preserves the angle between them, so one number
    describes both the channel-direction disagreement and the transect
    disagreement. Near 0 means the two transects sample the same line;
    near 90 means A's transect runs ALONG the channel B is crossing,
    which is the artifact this module exists to detect."""
    cross = first[0] * second[1] - first[1] * second[0]
    dot = first[0] * second[0] + first[1] * second[1]
    return round(abs(math.degrees(math.atan2(cross, dot))), 2)


def _walk_both_sides(dem: dict, station_xy: tuple, station_rowcol: tuple, direction: tuple) -> dict:
    """One transect: two ridge_crest_walk()s outward perpendicular to
    `direction`, with each side's crest height above THIS station's
    channel cell and the binding (lower) side.

    The 'left' ray is the +90-degree rotation of the direction, the same
    convention measure_valley_width() and build_embankment_compartment()
    both use, so A's sides here line up with the sides on the record."""
    perpendicular = (-direction[1], direction[0])
    left = ridge_crest_walk(dem, station_xy, perpendicular)
    right = ridge_crest_walk(dem, station_xy, (-perpendicular[0], -perpendicular[1]))
    left_height = crest_height_above_channel(dem, left, station_rowcol)
    right_height = crest_height_above_channel(dem, right, station_rowcol)
    return {
        "direction": direction,
        "bearing_deg": bearing_degrees(direction),
        "left": left,
        "right": right,
        "left_height_m": left_height,
        "right_height_m": right_height,
        "min_height_m": lower_crest_height(left_height, right_height),
        "width_m": round(left["half_width_m"] + right["half_width_m"], 1),
        "bound_hit": left["bound_hit"] or right["bound_hit"],
    }


def _classify(a_min: Optional[float], b_min: Optional[float]) -> str:
    """One station's verdict contribution, over the two methods' BINDING
    depths.

    ABSENCE IS A RESULT, NOT A GAP. A station where A found no crest on
    either side but B did has been RECOVERED by the re-bearing, and that
    is the strongest single piece of evidence for the artifact -- so it
    counts as deeper under B however large the recovered depth is. The
    mirror case counts the other way. Only "neither method measured
    anything" is set aside, because it says nothing about bearing."""
    if a_min is None and b_min is None:
        return NEITHER_MEASURED
    if a_min is None:
        return DEEPER_UNDER_B
    if b_min is None:
        return DEEPER_UNDER_A
    if b_min - a_min > VERDICT_MARGIN_METERS:
        return DEEPER_UNDER_B
    if a_min - b_min > VERDICT_MARGIN_METERS:
        return DEEPER_UNDER_A
    return COMPARABLE


def compare_compartment_bearings(dem: dict, zone: dict) -> dict:
    """The A/B comparison for ONE compartment, at both of its transect
    stations.

    The stem handed to the secant is the pinch walk's own station list,
    reversed: local_stem_direction() documents its input as ordered
    DOWNSTREAM-FIRST (downstream is decreasing index), while the walk
    records its stations upstream-first from the seed. Reversing rather
    than negating the result keeps this module inside that function's
    stated contract instead of second-guessing its sign.
    """
    stations_rowcol = [tuple(station["rowcol"]) for station in zone["walk_stations"]]
    # Downstream-first, per local_stem_direction()'s contract.
    stem = list(reversed(stations_rowcol))

    seed_rowcol = tuple(zone["seed"]["rowcol"])
    pinch_rowcol = tuple(zone["pinch"]["rowcol"])
    seed_xy = pixel_center_xy(dem, *seed_rowcol)
    pinch_xy = pixel_center_xy(dem, *pinch_rowcol)

    baseline = (pinch_xy[0] - seed_xy[0], pinch_xy[1] - seed_xy[1])
    baseline_length = math.hypot(*baseline)
    baseline_unit = (baseline[0] / baseline_length, baseline[1] / baseline_length)

    results = []
    for end_name, rowcol, station_xy in (
        ("seed", seed_rowcol, seed_xy),
        ("pinch", pinch_rowcol, pinch_xy),
    ):
        try:
            stem_index = stem.index(rowcol)
        except ValueError:
            # Cannot happen by construction (both stations are walked
            # cells) -- reported rather than crashed, so one odd
            # compartment cannot take the whole instrument down.
            results.append({"end": end_name, "rowcol": rowcol, "error": "station not on the walked stem"})
            continue
        local_direction, degenerate = local_stem_direction(dem, stem, stem_index)
        # A one-sided window is the normal case at the seed (the stem
        # starts there) -- recorded so a seed-station disagreement can be
        # read with the weaker estimate in mind.
        window = STEM_DIRECTION_WINDOW_CELLS
        one_sided = stem_index + window > len(stem) - 1 or stem_index - window < 0
        results.append(
            {
                "end": end_name,
                "rowcol": rowcol,
                "stem_index": stem_index,
                "stem_length": len(stem),
                "secant_one_sided": one_sided,
                "secant_degenerate": degenerate,
                METHOD_BASELINE: _walk_both_sides(dem, station_xy, rowcol, baseline_unit),
                METHOD_LOCAL: _walk_both_sides(dem, station_xy, rowcol, local_direction),
                "angle_deg": _angle_between_degrees(baseline_unit, local_direction),
            }
        )
        results[-1]["classification"] = _classify(
            results[-1][METHOD_BASELINE]["min_height_m"],
            results[-1][METHOD_LOCAL]["min_height_m"],
        )
        # THE LIKE-FOR-LIKE CHECK: method A, recomputed here, must
        # reproduce the heights production stored. If it does not, the
        # comparison below is not comparing what it claims to and the
        # line says so rather than quietly reporting a difference that
        # is really a bug in this instrument.
        stored = (
            zone[f"{end_name}_crest_height_left_m"],
            zone[f"{end_name}_crest_height_right_m"],
            zone[f"{end_name}_crest_height_min_m"],
        )
        recomputed = (
            results[-1][METHOD_BASELINE]["left_height_m"],
            results[-1][METHOD_BASELINE]["right_height_m"],
            results[-1][METHOD_BASELINE]["min_height_m"],
        )
        results[-1]["reproduces_record"] = stored == recomputed
        results[-1]["stored_heights"] = stored

    return {
        "zone_id": zone["id"],
        "baseline_bearing_deg": bearing_degrees(baseline_unit),
        "baseline_length_m": round(baseline_length, 1),
        "stations": results,
    }


def _height_cell(value: Optional[float]) -> str:
    """A height with its unit, or the absent sentinel WITHOUT one --
    "absent m" would read as a quantity, which is the one thing this
    column must never suggest."""
    return f"{'absent':>9}" if value is None else f"{value:7.2f} m"


def _bound_cell(walk: dict) -> str:
    """How a side ended: the crest's distance, or why there wasn't one.
    'bound' means it ran the full RIDGE_WALK_MAX_HALF_WIDTH_METERS;
    'edge' means it left the grid (or hit nodata) sooner, which is a
    different problem and must not be read as the bound being too
    short."""
    if not walk["bound_hit"]:
        return f"crest@{walk['half_width_m']:.1f}m"
    if walk["half_width_m"] >= RIDGE_WALK_MAX_HALF_WIDTH_METERS:
        return f"bound@{walk['half_width_m']:.1f}m"
    return f"edge@{walk['half_width_m']:.1f}m"


def _absent_flank_tally(comparisons: list[dict]) -> dict:
    """Across both methods: how many flanks came back absent, and
    whether they gave up AT the half-width bound or earlier at the grid
    edge. The distinction decides a separate question -- a bound that is
    too short for this terrain is a small branch of its own, while a
    grid edge is a DEM-extent problem and neither is a terrain finding."""
    tally = {}
    for method in (METHOD_BASELINE, METHOD_LOCAL):
        absent = at_bound = at_edge = total = 0
        distances = []
        for comparison in comparisons:
            for station in comparison["stations"]:
                if "error" in station:
                    continue
                for side in ("left", "right"):
                    walk = station[method][side]
                    total += 1
                    if not walk["bound_hit"]:
                        continue
                    absent += 1
                    distances.append(walk["half_width_m"])
                    if walk["half_width_m"] >= RIDGE_WALK_MAX_HALF_WIDTH_METERS:
                        at_bound += 1
                    else:
                        at_edge += 1
        tally[method] = {
            "absent": absent,
            "total": total,
            "at_bound": at_bound,
            "at_edge": at_edge,
            "max_distance_m": max(distances) if distances else None,
        }
    return tally


def _zero_height_audit(dem: dict, comparisons: list[dict]) -> list[dict]:
    """Every reading at or below 0.00 m, with the crest cell beside the
    station cell and the height RE-DERIVED from raw elevations.

    WHAT THE AUDIT SETTLES, part one. ridge_crest_walk() seeds its
    running maximum with the START cell, so ground that falls a full
    prominence immediately off the station declares the crest AT the
    station: the crest's rowcol EQUALS the station's, and 0.00 m
    correctly means "no shoulder above the channel at all". If a zero
    or negative reading comes back with the crest somewhere ELSE, that
    mechanism is not what produced it and the number is a defect.

    PART TWO, WHICH IS WHY THE RAW RE-DERIVATION IS HERE. A coincident
    crest should give EXACTLY 0.0, because both sides of the
    subtraction are the same cell's elevation. It does not, and the
    reason is a DOUBLE ROUNDING in crest_height_above_channel():
    ridge_crest_walk() returns crest_elevation_m already rounded to 2
    dp, and the channel elevation is read UNROUNDED off the raw array,
    so the difference is round(round(e, 2) - e, 2) -- up to +/-0.005 m,
    signed either way, which is exactly how a -0.00 appears where the
    true answer is 0. The same +/-5 mm rides EVERY crest height the
    field reports, not only the zeros. raw_difference_m below is the
    honest value (both sides unrounded); stored_height_m is what the
    field says."""
    rows = []
    for comparison in comparisons:
        for station in comparison["stations"]:
            if "error" in station:
                continue
            for method in (METHOD_BASELINE, METHOD_LOCAL):
                for side in ("left", "right"):
                    height = station[method][f"{side}_height_m"]
                    if height is None or height > 0.0:
                        continue
                    crest_rowcol = station[method][side]["crest_rowcol"]
                    crest_rowcol = tuple(crest_rowcol) if crest_rowcol is not None else None
                    raw_difference = None
                    if crest_rowcol is not None:
                        raw_difference = float(dem["array"][crest_rowcol]) - float(
                            dem["array"][station["rowcol"]]
                        )
                    rows.append(
                        {
                            "zone_id": comparison["zone_id"],
                            "end": station["end"],
                            "method": method,
                            "side": side,
                            "height_m": height,
                            "station_rowcol": station["rowcol"],
                            "crest_rowcol": crest_rowcol,
                            "coincident": crest_rowcol == station["rowcol"],
                            "raw_difference_m": raw_difference,
                        }
                    )
    return rows


def _negative_zero(value: Optional[float]) -> bool:
    """True for -0.0 and False for +0.0. Needed because -0.0 == 0.0 is
    True in Python, so an equality test can never see the sign that is
    the whole point of the -0.00 question."""
    return value is not None and value == 0.0 and math.copysign(1.0, value) < 0.0


def _rounding_audit(dem: dict, comparisons: list[dict]) -> dict:
    """How far every MEASURED crest height sits from the honest raw
    difference (crest cell's raw elevation minus the station cell's,
    neither pre-rounded), across both methods.

    This is a check on the FIELD, not on the bearing: it is here because
    the -0.00 on the record is the visible corner of it, and a reader
    asked to believe a depth comparison is owed the size of the error
    riding every number in it."""
    measured = mismatched = negative_zeros = 0
    worst = 0.0
    for comparison in comparisons:
        for station in comparison["stations"]:
            if "error" in station:
                continue
            for method in (METHOD_BASELINE, METHOD_LOCAL):
                for side in ("left", "right"):
                    height = station[method][f"{side}_height_m"]
                    crest_rowcol = station[method][side]["crest_rowcol"]
                    if height is None or crest_rowcol is None:
                        continue
                    measured += 1
                    raw = float(dem["array"][tuple(crest_rowcol)]) - float(
                        dem["array"][station["rowcol"]]
                    )
                    honest = round(raw, 2)
                    if honest != height or (_negative_zero(height) and raw == 0.0):
                        mismatched += 1
                    if _negative_zero(height) and raw == 0.0:
                        negative_zeros += 1
                    worst = max(worst, abs(height - honest))
    return {
        "measured": measured,
        "mismatched": mismatched,
        "negative_zeros": negative_zeros,
        "worst_error_m": round(worst, 6),
    }


def decide_verdict(comparisons: list[dict]) -> dict:
    """The pinned rule, applied. See VERDICT_MARGIN_METERS."""
    counts = {DEEPER_UNDER_B: 0, DEEPER_UNDER_A: 0, COMPARABLE: 0, NEITHER_MEASURED: 0}
    for comparison in comparisons:
        for station in comparison["stations"]:
            if "error" in station:
                continue
            counts[station["classification"]] += 1
    total = sum(counts.values())
    if total == 0:
        return {"verdict": VERDICT_MIXED, "counts": counts, "total": 0, "reason": "no stations compared"}
    threshold = total * VERDICT_MAJORITY_FRACTION
    if counts[DEEPER_UNDER_B] > threshold:
        verdict, reason = (
            VERDICT_BEARING_ARTIFACT,
            f"{counts[DEEPER_UNDER_B]} of {total} stations read more than "
            f"{VERDICT_MARGIN_METERS} m deeper under the local-channel perpendicular",
        )
    elif counts[COMPARABLE] > threshold:
        verdict, reason = (
            VERDICT_TERRAIN,
            f"{counts[COMPARABLE]} of {total} stations agree within {VERDICT_MARGIN_METERS} m "
            "between the two bearings",
        )
    else:
        verdict, reason = (
            VERDICT_MIXED,
            f"neither outcome holds a majority of {total} stations "
            f"(deeper under B {counts[DEEPER_UNDER_B]}, comparable {counts[COMPARABLE]}, "
            f"deeper under A {counts[DEEPER_UNDER_A]}, neither measured {counts[NEITHER_MEASURED]})",
        )
    return {"verdict": verdict, "counts": counts, "total": total, "reason": reason}


def summarize_transect_bearing_comparison(dem: dict, identify_result: dict) -> str:
    """The whole instrument as terminal text: per-compartment bearings,
    per-station A/B readings, the absent-flank tally, the zero-height
    audit, and the pinned verdict."""
    zones = identify_result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]
    lines = [
        "=== TRANSECT BEARING A/B: IS THE SHALLOW ENCLOSURE AN ARTIFACT OF WHERE WE POINTED? ===",
        "  A = baseline-perpendicular (production today). B = local-channel-perpendicular",
        f"  (valley_level_pool.local_stem_direction(), the de-quantized secant over "
        f"+/-{STEM_DIRECTION_WINDOW_CELLS} channel cells).",
        f"  Same crest walk, same {RIDGE_PROMINENCE_METERS} m prominence, same "
        f"{RIDGE_WALK_MAX_HALF_WIDTH_METERS} m half-width bound, same absent semantics --",
        "  ONLY the bearing differs, which is what makes any difference attributable to it.",
        f"  PINNED IN ADVANCE: a station is materially deeper under B when its binding depth beats "
        f"A's by more than {VERDICT_MARGIN_METERS} m",
        "  (or B measures where A found nothing); BEARING ARTIFACT needs a majority of stations, "
        "TERRAIN needs a majority comparable, else MIXED.",
        "",
        "  NOTE: walk_embankment_pinch() measures its own widths off _flow_direction_unit(), the RAW",
        "  D8 STEP (8 directions, up to 22.5 deg of quantization error) -- NOT a secant. The secant "
        "reused here is",
        "  the level-pool arc's. Rows below marked one-sided have a clamped (downstream-only) "
        "secant window.",
        "",
    ]
    if not zones:
        lines.append("  (no surviving compartment on this run -- the comparison has nothing to say)")
        return "\n".join(lines)

    comparisons = [compare_compartment_bearings(dem, zone) for zone in zones]

    for comparison, zone in zip(comparisons, zones):
        lines.append(
            f"  zone {comparison['zone_id']}: baseline bearing "
            f"{comparison['baseline_bearing_deg']:.2f} deg over "
            f"{comparison['baseline_length_m']:.1f} m, pinch width "
            f"{zone['pinch']['width_m']:.1f} m, catchment "
            f"{zone['pinch_catchment_acres']:.2f} ac"
        )
        for station in comparison["stations"]:
            if "error" in station:
                lines.append(f"      {station['end']:>5}: {station['error']}")
                continue
            local = station[METHOD_LOCAL]
            base = station[METHOD_BASELINE]
            notes = []
            if station["secant_one_sided"]:
                notes.append("one-sided secant")
            if station["secant_degenerate"]:
                notes.append("DEGENERATE secant")
            if not station["reproduces_record"]:
                notes.append(
                    f"!! method A does NOT reproduce the record {station['stored_heights']} -- "
                    "comparison unreliable"
                )
            note = f"   [{'; '.join(notes)}]" if notes else ""
            lines.append(
                f"      {station['end']:>5} @{station['rowcol']}: local channel bearing "
                f"{local['bearing_deg']:.2f} deg vs baseline "
                f"{comparison['baseline_bearing_deg']:.2f} deg -> ANGLE "
                f"{station['angle_deg']:.2f} deg{note}"
            )
            for label, method in (("A", METHOD_BASELINE), ("B", METHOD_LOCAL)):
                data = station[method]
                lines.append(
                    f"        {label}  L {_height_cell(data['left_height_m'])} "
                    f"({_bound_cell(data['left'])})   "
                    f"R {_height_cell(data['right_height_m'])} "
                    f"({_bound_cell(data['right'])})   "
                    f"MIN {_height_cell(data['min_height_m'])}   width {data['width_m']:.1f} m"
                )
            lines.append(f"        -> {station['classification']}")

    # --- the absent-flank question ---
    tally = _absent_flank_tally(comparisons)
    lines.append("")
    lines.append("  ABSENT FLANKS (a walk that never declared a crest), by method:")
    for method in (METHOD_BASELINE, METHOD_LOCAL):
        entry = tally[method]
        recovered = ""
        if method == METHOD_LOCAL:
            delta = tally[METHOD_BASELINE]["absent"] - entry["absent"]
            recovered = f"  -- B recovers {delta} flank(s) A could not resolve" if delta else ""
        lines.append(
            f"    {method:>22}: {entry['absent']} of {entry['total']} flanks absent "
            f"({entry['at_bound']} ran the full {RIDGE_WALK_MAX_HALF_WIDTH_METERS} m bound, "
            f"{entry['at_edge']} left the grid sooner){recovered}"
        )
    bound_binding = tally[METHOD_BASELINE]["at_bound"] + tally[METHOD_LOCAL]["at_bound"]
    edge_binding = tally[METHOD_BASELINE]["at_edge"] + tally[METHOD_LOCAL]["at_edge"]
    if bound_binding or edge_binding:
        lines.append(
            f"    IS THE {RIDGE_WALK_MAX_HALF_WIDTH_METERS} m BOUND BINDING? "
            f"{bound_binding} absent flank(s) ran it out against {edge_binding} that hit the grid "
            "edge first."
            + (
                "  A bound-dominated tally means the bound may be too short for this terrain -- "
                "its own small branch, not a terrain finding."
                if bound_binding > edge_binding
                else "  Edge-dominated: this is DEM extent, not the bound."
            )
        )
    else:
        lines.append("    (no absent flanks under either method)")

    # --- the zero/negative readings ---
    zero_rows = _zero_height_audit(dem, comparisons)
    lines.append("")
    lines.append("  READINGS AT OR BELOW 0.00 m (crest cell vs station cell):")
    if not zero_rows:
        lines.append("    (none)")
    for row in zero_rows:
        verdict = (
            "crest AT the station -- correct: no shoulder above the channel at all"
            if row["coincident"]
            else "!! DEFECT: crest is NOT the station cell, so the station-seeded mechanism did not "
            "produce this"
        )
        raw = row["raw_difference_m"]
        raw_note = f", raw difference {raw:+.6f} m" if raw is not None else ""
        if raw == 0.0 and _negative_zero(row["height_m"]):
            raw_note += " (EXACTLY zero -- the stored MINUS sign is a double-rounding artifact)"
        lines.append(
            f"    zone {row['zone_id']} {row['end']} {row['method']} {row['side']}: "
            f"{row['height_m']:+.2f} m, station {row['station_rowcol']} vs crest "
            f"{row['crest_rowcol']}{raw_note} -- {verdict}"
        )

    # --- the double-rounding defect, measured across EVERY reading ---
    rounding = _rounding_audit(dem, comparisons)
    lines.append("")
    lines.append(
        f"  ROUNDING AUDIT: {rounding['mismatched']} of {rounding['measured']} measured reading(s) "
        f"disagree with the raw crest-minus-channel difference; worst {rounding['worst_error_m']:.4f} m"
        + (f", {rounding['negative_zeros']} stored as -0.00 where the truth is exactly 0"
           if rounding["negative_zeros"] else "")
    )
    if rounding["mismatched"]:
        lines.append(
            "    DEFECT (reported, NOT fixed on this diagnostic branch): "
            "crest_height_above_channel() subtracts the UNROUNDED raw channel elevation from"
        )
        lines.append(
            "    ridge_crest_walk()'s ALREADY-ROUNDED crest_elevation_m, so every crest height on "
            "the record carries up to +/-0.005 m of double-rounding error and a crest coincident "
            "with its station can read -0.00."
        )
        lines.append(
            f"    Sub-centimetre, so it moves no verdict here (the margin is "
            f"{VERDICT_MARGIN_METERS} m); it is a one-line fix -- read the crest elevation raw at "
            "crest_rowcol -- and belongs on its own branch."
        )

    # --- the verdict ---
    verdict = decide_verdict(comparisons)
    lines.append("")
    lines.append(f"  VERDICT: {verdict['verdict']} -- {verdict['reason']}")
    lines.append(
        {
            VERDICT_BEARING_ARTIFACT: "    The transects are mis-oriented. The fix is to take crest "
            "walks along the LOCAL CHANNEL perpendicular; that is its own branch, not this one.",
            VERDICT_TERRAIN: "    The bearing is not the problem: re-pointing the transects at the "
            "local channel does not change what they find, so the depths on the record are what this "
            "ground offers at these stations.",
            VERDICT_MIXED: "    Reported as mixed rather than forced either way. Neither the "
            "re-bearing nor the terrain reading explains a majority of stations.",
        }[verdict["verdict"]]
    )
    # THE CONSEQUENCE IS STATED ONLY WHERE IT APPLIES. "Terrain" exonerates
    # the bearing; it does NOT by itself say the sites are undammable --
    # that depends on how deep the agreed readings actually are, which is
    # measured here rather than assumed from the verdict.
    if verdict["verdict"] == VERDICT_TERRAIN:
        binding = [
            station[METHOD_LOCAL]["min_height_m"]
            for comparison in comparisons
            for station in comparison["stations"]
            if "error" not in station and station[METHOD_LOCAL]["min_height_m"] is not None
        ]
        shallow = [depth for depth in binding if depth < VERDICT_MARGIN_METERS]
        if binding and len(shallow) > len(binding) / 2:
            lines.append(
                f"    AND THE AGREED DEPTHS ARE SHALLOW: {len(shallow)} of {len(binding)} measured "
                f"binding shoulders stand under {VERDICT_MARGIN_METERS} m above their channel. On "
                "these sites the finding is that there is no impoundable"
            )
            lines.append(
                "    cross-section to find -- whether the embankment class is viable here regardless "
                "of catchment becomes a scoring/narrative decision, not a geometry fix."
            )
        elif binding:
            lines.append(
                f"    The agreed depths are not shallow on this run ({len(binding) - len(shallow)} "
                f"of {len(binding)} binding shoulders at or above {VERDICT_MARGIN_METERS} m), so "
                "this verdict exonerates the bearing and says nothing further."
            )
    return "\n".join(lines)
