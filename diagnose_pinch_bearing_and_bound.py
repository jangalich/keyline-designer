"""
diagnose_pinch_bearing_and_bound.py

THE THREE-WAY ATTRIBUTION TABLE and the HALF-WIDTH BOUND SWEEP: two
changes to what the pinch walk MEASURES, reported so a reader can tell
which one moved which number.

WHY THEY SHIP TOGETHER AND ARE STILL SEPARABLE. Both alter the width
profile, so both need the same before/after evidence -- pinch cells,
widths, catchments, compartment acreages, absent-flank tallies -- and
running them apart would mean producing that table twice and reading the
second diff against a baseline that had just moved. They stay separable
in the reading because their SIGNATURES DIFFER: re-bearing shifts widths
and can relocate a pinch while leaving the absent-flank count roughly
alone, whereas a longer half-width bound converts absent flanks into
measured ones and can only INCREASE widths where it fires.

THE THREE CONFIGURATIONS, and the two subtractions that matter:

    1  D8 bearing     + 100 m bound   the record as it stood
    2  secant bearing + 100 m bound   the BEARING's effect, alone
    3  secant bearing + each swept bound   the BOUND's effect, on top

Column 2 minus column 1 is attributable to the bearing. Column 3 minus
column 2 is attributable to the bound. The table prints both deltas
rather than leaving a reader to subtract.

ONE OF THESE IS A PRODUCTION CHANGE AND ONE IS NOT, which is the most
important sentence in this module. The BEARING has shipped:
walk_embankment_pinch() now measures perpendicular to
local_stem_direction()'s de-quantized secant, and configuration 1 is
reachable only through the PINCH_BEARING_D8 escape hatch that exists for
this table. The BOUND has not and does not ship here: this module SWEEPS
RIDGE_WALK_MAX_HALF_WIDTH_METERS and reports a curve so a value can be
chosen from evidence. It picks nothing. If the absent-flank count falls
off sharply by 150 m that suggests a value; if absences persist at 200 m
the flanks are genuinely unbounded hillside and the crest concept needs
rethinking rather than extending -- and this module says which the data
shows, without choosing.

A CAVEAT THE SWEEP CANNOT ESCAPE, stated where the numbers are. The DEM
is fetched over the boundary bbox plus a fixed margin, so a walk given a
longer bound runs out of GRID before it runs out of bound. Flanks that
give up at the grid edge are a DEM-extent limit, not a terrain finding
and not evidence about the bound -- the tallies below split the two at
every swept value, and the edge share will grow as the bound does. A
sweep value whose absences are mostly grid-edge has told you nothing
about terrain.
"""

from typing import Optional

from water_survey_areas import (
    PINCH_BEARING_D8,
    PINCH_BEARING_SECANT,
    RIDGE_WALK_MAX_HALF_WIDTH_METERS,
    generate_embankment_compartments,
)

# The swept values. DIAGNOSTIC-ONLY: this list is read by this module and
# nothing else, and no entry of it is a candidate that has been chosen.
# 100 is the shipped RIDGE_WALK_MAX_HALF_WIDTH_METERS, present so the
# sweep contains its own baseline.
HALF_WIDTH_SWEEP_METERS = (100.0, 150.0, 200.0)

CONFIG_BASELINE = "1 D8 + 100 m"


def _config_label(bearing: str, bound: float) -> str:
    return f"{'secant' if bearing == PINCH_BEARING_SECANT else 'D8'} + {bound:.0f} m"


def _gate_context(result: dict) -> dict:
    """The gate context the embankment pass ran on, ASSEMBLED FROM THE
    RUN'S OWN OBJECTS rather than recomputed: every value here is a
    reference to an array the result already carries, so a
    re-run reads the identical grids the real pass read. Recomputing any
    of them would make a difference in the table a difference in the
    scaffolding."""
    return {
        "twi_score": result["screens"]["twi_score"],
        "depression_depth": result["screens"]["depression_depth"],
        "flow_accumulation": result["screens"]["flow_accumulation"],
        "slope_pct": result["screens"]["slope_pct"],
        "soil_covered_mask": result["soil"]["covered_mask"],
        "soil_checked": result["soil_checked"],
    }


def run_configuration(dem: dict, result: dict, bearing: str, bound: float) -> dict:
    """One full embankment generation pass over the run's OWN inputs,
    with only the bearing and the half-width bound varied.

    Every array, mask, surface and polygon comes off the result dict the
    real run returned -- not rebuilt here. That is the whole validity of
    the comparison: if this function re-derived the flow field or the
    gate mask, a difference in the table could be a difference in the
    scaffolding rather than in the thing being measured."""
    compartments, _seeds = generate_embankment_compartments(
        dem,
        result["surfaces"],
        result["gate_mask"],
        result["on_parcel_mask"],
        result["road_cell_mask"],
        result["road_union_utm"],
        result["boundary_polygon_utm"],
        result["flow_to_row"],
        result["flow_to_col"],
        _gate_context(result),
        max_half_width_meters=bound,
        direction_mode=bearing,
    )
    return {
        "bearing": bearing,
        "bound_m": bound,
        "label": _config_label(bearing, bound),
        "compartments": compartments,
        "by_seed": {tuple(zone["seed"]["rowcol"]): zone for zone in compartments},
    }


def _absent_tally(configuration: dict, bound: float) -> dict:
    """Absent flanks across every WALKED STATION of every compartment in
    one configuration, split by why the walk gave up.

    RAN-THE-BOUND means the walk used its whole half-width allowance on
    rising-or-level ground: the bound is what stopped it, and a longer
    one might resolve the flank. LEFT-THE-GRID means it ran off the DEM
    or into nodata first: the bound is irrelevant there and a longer one
    cannot help, because the elevation data simply stops. Conflating
    them would read a DEM-extent limit as evidence about a constant."""
    absent = at_bound = at_edge = 0
    total = 0
    for zone in configuration["compartments"]:
        for station in zone["walk_stations"]:
            for side in ("left", "right"):
                walk = station["measurement"][side]
                total += 1
                if not walk["bound_hit"]:
                    continue
                absent += 1
                if walk["half_width_m"] >= bound:
                    at_bound += 1
                else:
                    at_edge += 1
    return {"absent": absent, "total": total, "at_bound": at_bound, "at_edge": at_edge}


def _binding_depths(zone: dict) -> tuple:
    return (zone["seed_crest_height_min_m"], zone["pinch_crest_height_min_m"])


def _depth_cell(value: Optional[float]) -> str:
    return "absent" if value is None else f"{value:.2f} m"


def _profile_digest(zone: dict, limit: int = 12) -> str:
    widths = [station["width_m"] for station in zone["walk_stations"]]
    pinch_index = next(
        (
            index
            for index, station in enumerate(zone["walk_stations"])
            if tuple(station["rowcol"]) == tuple(zone["pinch"]["rowcol"])
        ),
        None,
    )
    parts = []
    for index, width in enumerate(widths[:limit]):
        parts.append(f"|{width:.1f}|" if index == pinch_index else f"{width:.1f}")
    if len(widths) > limit:
        parts.append(f"... ({len(widths)} stations)")
    return " ".join(parts)


def _delta(new: Optional[float], old: Optional[float]) -> str:
    """A signed change, with absence handled as a transition rather than
    arithmetic: 'absent -> 2.31' says more than a blank would."""
    if old is None and new is None:
        return "absent both"
    if old is None:
        return f"absent -> {new:.2f}"
    if new is None:
        return f"{old:.2f} -> absent"
    difference = new - old
    return f"{difference:+.2f}" if difference else "unchanged"


def summarize_pinch_bearing_and_bound(dem: dict, result: dict) -> str:
    """The whole instrument as terminal text.

    `result` is the compute core's own return, which carries every array,
    mask, surface and polygon the embankment pass ran on."""
    lines = [
        "=== PINCH BEARING + HALF-WIDTH BOUND: THREE-WAY ATTRIBUTION ===",
        "  Configuration 1 is the record as it stood (D8 bearing, 100 m bound). 2 varies ONLY the",
        "  bearing, 3 varies ONLY the bound on top of 2, so (2 - 1) is the bearing's effect and",
        "  (3 - 2) is the bound's. Every configuration runs on this run's OWN arrays, masks,",
        "  surfaces and polygons -- nothing is rebuilt.",
        "",
        "  THE BEARING HAS SHIPPED: walk_embankment_pinch() measures perpendicular to the",
        "  de-quantized secant now, and the D8 column exists only through the escape hatch kept",
        "  for this table. THE BOUND HAS NOT: the sweep reports a curve and chooses nothing.",
        "",
    ]

    baseline = run_configuration(dem, result, PINCH_BEARING_D8, RIDGE_WALK_MAX_HALF_WIDTH_METERS)
    bearing_only = run_configuration(
        dem, result, PINCH_BEARING_SECANT, RIDGE_WALK_MAX_HALF_WIDTH_METERS
    )
    swept = [
        run_configuration(dem, result, PINCH_BEARING_SECANT, bound)
        for bound in HALF_WIDTH_SWEEP_METERS
        if bound != RIDGE_WALK_MAX_HALF_WIDTH_METERS
    ]
    configurations = [baseline, bearing_only] + swept

    # --- per-zone attribution, keyed by SEED cell ---
    # The seed is the stable identity across configurations: zone ids are
    # assigned per run and the pinch is exactly what may move, so keying
    # on either would compare a zone against a different zone.
    seeds = sorted(
        {seed for configuration in configurations for seed in configuration["by_seed"]}
    )
    lines.append(f"  {len(seeds)} seed(s) built a compartment in at least one configuration.")
    lines.append("")
    for seed in seeds:
        lines.append(f"  SEED {seed}:")
        reference = baseline["by_seed"].get(seed)
        previous = reference
        for configuration in configurations:
            zone = configuration["by_seed"].get(seed)
            if zone is None:
                lines.append(
                    f"    {configuration['label']:>16}: no compartment "
                    "(this seed's walk found no constriction in this configuration)"
                )
                continue
            seed_depth, pinch_depth = _binding_depths(zone)
            moved = ""
            if previous is not None and configuration is not previous:
                if tuple(zone["pinch"]["rowcol"]) != tuple(previous["pinch"]["rowcol"]):
                    moved = (
                        f"   PINCH MOVED {tuple(previous['pinch']['rowcol'])} -> "
                        f"{tuple(zone['pinch']['rowcol'])}"
                    )
            lines.append(
                f"    {configuration['label']:>16}: pinch {tuple(zone['pinch']['rowcol'])} "
                f"w {zone['pinch']['width_m']:.1f} m, catchment "
                f"{zone['pinch_catchment_acres']:.2f} ac, band "
                f"{zone['compartment_footprint_acres']:.4f} ac, hull {zone['zone_acres']:.4f} ac, "
                f"depths seed {_depth_cell(seed_depth)} / pinch {_depth_cell(pinch_depth)}{moved}"
            )
            lines.append(f"                      profile {_profile_digest(zone)}")
            previous = zone
        # The two attributions, stated rather than left to be subtracted.
        before = baseline["by_seed"].get(seed)
        middle = bearing_only["by_seed"].get(seed)
        if before is not None and middle is not None:
            lines.append(
                f"      BEARING (2-1): width {middle['pinch']['width_m'] - before['pinch']['width_m']:+.1f} m, "
                f"catchment {middle['pinch_catchment_acres'] - before['pinch_catchment_acres']:+.2f} ac, "
                f"hull {middle['zone_acres'] - before['zone_acres']:+.4f} ac, "
                f"pinch depth {_delta(middle['pinch_crest_height_min_m'], before['pinch_crest_height_min_m'])}"
                + (
                    ""
                    if tuple(middle["pinch"]["rowcol"]) == tuple(before["pinch"]["rowcol"])
                    else "  [PINCH CELL RELOCATED]"
                )
            )
        for configuration in swept:
            after = configuration["by_seed"].get(seed)
            if middle is None or after is None:
                continue
            lines.append(
                f"      BOUND  (3-2) at {configuration['bound_m']:.0f} m: "
                f"width {after['pinch']['width_m'] - middle['pinch']['width_m']:+.1f} m, "
                f"hull {after['zone_acres'] - middle['zone_acres']:+.4f} ac, "
                f"pinch depth {_delta(after['pinch_crest_height_min_m'], middle['pinch_crest_height_min_m'])}"
                + (
                    ""
                    if tuple(after["pinch"]["rowcol"]) == tuple(middle["pinch"]["rowcol"])
                    else f"  [PINCH CELL RELOCATED -> {tuple(after['pinch']['rowcol'])}]"
                )
            )
        lines.append("")

    # --- the absent-flank curve: THE OUTPUT of the sweep ---
    lines.append("  ABSENT-FLANK CURVE (every walked station, both flanks, per configuration):")
    curve = []
    for configuration in configurations:
        tally = _absent_tally(configuration, configuration["bound_m"])
        curve.append((configuration, tally))
        share = (100.0 * tally["absent"] / tally["total"]) if tally["total"] else 0.0
        lines.append(
            f"    {configuration['label']:>16}: {tally['absent']:>4} of {tally['total']:>4} flanks "
            f"absent ({share:5.1f}%) -- {tally['at_bound']} ran the bound, "
            f"{tally['at_edge']} left the grid"
        )

    # The reading, keyed off the swept values only (configuration 1 and 2
    # share a bound, so the bearing pair is not part of this curve's
    # question).
    swept_tallies = [
        (configuration, tally)
        for configuration, tally in curve
        if configuration["bearing"] == PINCH_BEARING_SECANT
    ]
    lines.append("")
    if len(swept_tallies) >= 2:
        first, last = swept_tallies[0], swept_tallies[-1]
        first_bound_absences = first[1]["at_bound"]
        last_bound_absences = last[1]["at_bound"]
        lines.append(
            f"  WHAT THE CURVE SAYS: flanks stopped BY THE BOUND go "
            f"{first_bound_absences} -> {last_bound_absences} between "
            f"{first[0]['bound_m']:.0f} m and {last[0]['bound_m']:.0f} m, while flanks stopped by "
            f"the GRID EDGE go {first[1]['at_edge']} -> {last[1]['at_edge']}."
        )
        if first_bound_absences == 0:
            lines.append(
                "    The bound was never binding on this run, so the sweep has nothing to say "
                "about its value here."
            )
        elif last_bound_absences == 0:
            lines.append(
                "    Every bound-stopped flank resolves by the last swept value: the bound WAS "
                "too short for this terrain, and the curve suggests where a new one would sit. "
                "It is still not chosen here."
            )
        elif last_bound_absences >= first_bound_absences * 0.5:
            lines.append(
                "    Absences PERSIST as the bound grows: these flanks are unbounded hillside "
                "rather than a shoulder just out of reach, and extending the bound is not the "
                "answer -- the crest concept itself needs rethinking. Nothing is chosen here."
            )
        else:
            lines.append(
                "    Absences fall off but do not vanish: part of the tally is a bound that was "
                "too short and part is genuinely unbounded ground. The split is above; no value "
                "is chosen here."
            )
        if last[1]["at_edge"] > last[1]["at_bound"]:
            lines.append(
                "    NOTE: at the widest swept value most absences are GRID EDGE, not bound. That "
                "is the DEM window (boundary bbox plus a fixed margin) running out, so those "
                "flanks say nothing about terrain and nothing about the constant."
            )

    # --- the question the previous branch's verdict left open ---
    lines.append("")
    crossings = []
    for configuration in configurations:
        for zone in configuration["compartments"]:
            for end, depth in zip(("seed", "pinch"), _binding_depths(zone)):
                if depth is not None and depth >= 1.0:
                    crossings.append((configuration["label"], zone["seed"]["rowcol"], end, depth))
    crossing_seeds = sorted({tuple(seed) for _label, seed, _end, _depth in crossings})
    lines.append(
        "  DOES ANY BINDING DEPTH CROSS 1.0 m UNDER ANY CONFIGURATION? "
        + (
            "NO -- every binding shoulder on every configuration stands under a metre."
            if not crossings
            else f"YES -- {len(crossings)} station-configuration(s) across "
            f"{len(crossing_seeds)} seed(s), deepest "
            f"{max(depth for _l, _s, _e, depth in crossings):.2f} m:"
        )
    )
    for seed in crossing_seeds[:8]:
        deepest = max(
            depth for _label, crossing_seed, _end, depth in crossings if tuple(crossing_seed) == seed
        )
        lines.append(f"    seed {seed}: deepest binding shoulder {deepest:.2f} m")
    if len(crossing_seeds) > 8:
        lines.append(f"    ... and {len(crossing_seeds) - 8} more seed(s)")
    lines.append(
        "    Neither change here is expected to find a dammable site that was hiding -- enclosure "
        "depths were confirmed shallow under BOTH transect bearings, and the TERRAIN verdict "
        "stands. This is about measuring the right cell,"
    )
    lines.append(
        "    not rescuing the embankment class. A crossing would be a surprise worth naming, not "
        "a reason to revise that verdict."
    )
    return "\n".join(lines)
