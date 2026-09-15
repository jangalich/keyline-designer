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

BOTH CHANGES HAVE NOW SHIPPED, and the escape hatches that reach their
retired forms exist for this table and nowhere else. The BEARING shipped
first: walk_embankment_pinch() measures perpendicular to
local_stem_direction()'s de-quantized secant, and the D8 columns are
reachable only through PINCH_BEARING_D8. The BOUND shipped second, and
this module's own sweep is what chose it -- on the reference property
absent flanks ran 107 / 29 / 8 at 100 / 150 / 200 m with ZERO stopped by
the grid edge at any value, so the old 100 m cap was suppressing roughly
four-fifths of the crest measurements the DEM window could support. 150 m
is the knee: 5.6% absent, most of the benefit, and the 1.5% still absent
at 200 m are very likely unbounded hillside rather than a shoulder just
out of reach.

THE SWEEP STILL PRINTS, at 100/150/200, and still brackets whatever is
shipped. That is deliberate: a constant chosen from a curve on ONE
property stays honest only while the curve keeps being drawn, so this
module reports the evidence on every run rather than leaving the value
to become folklore. It still chooses nothing by itself.

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

from valley_level_pool import POOL_REFERENCE_HEIGHT_METERS
from water_survey_areas import (
    DAM_SITE_HEIGHT_EXPONENT,
    DAM_SITE_HEIGHT_EXPONENT_LINEAR,
    DAM_SITE_HEIGHT_EXPONENT_STORAGE,
    DAM_SITE_SELECTION_MIN_WIDTH,
    DAM_SITE_SELECTION_RATIO,
    PINCH_BEARING_D8,
    PINCH_BEARING_SECANT,
    MIN_BINDING_SHOULDER_METERS,
    REASON_SHOULDER_BELOW_MINIMUM,
    RIDGE_WALK_MAX_HALF_WIDTH_METERS,
    SURVEY_TYPE_EMBANKMENT,
    generate_embankment_compartments,
)

# The swept values. DIAGNOSTIC-ONLY: this list is read by this module and
# nothing else. It spans the RETIRED bound, the SHIPPED one, and the next
# step up, so the curve that chose 150 keeps printing on every run and
# the choice stays re-measurable rather than becoming folklore.
HALF_WIDTH_SWEEP_METERS = (100.0, 150.0, 200.0)

# THE RETIRED BOUND, named so the attribution table can label its own
# before-column honestly. Production's value is
# RIDGE_WALK_MAX_HALF_WIDTH_METERS; this is what it was until the sweep's
# curve replaced it, and it is a constant HERE and nowhere else -- no
# production path may reach for it.
RETIRED_HALF_WIDTH_BOUND_METERS = 100.0


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


def run_objective(
    dem: dict,
    result: dict,
    selection_mode: str,
    height_exponent: int = DAM_SITE_HEIGHT_EXPONENT,
    label: Optional[str] = None,
) -> dict:
    """One full embankment generation pass under ONE dam-site objective,
    on the run's own inputs with the bearing and the bound held at their
    shipped values.

    Same reuse rule as run_configuration(): every array, mask, surface
    and polygon comes off the result dict the real run returned, so the
    only thing differing between objectives is the objective."""
    compartments, seeds = generate_embankment_compartments(
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
        selection_mode=selection_mode,
        height_exponent=height_exponent,
    )
    if label is None:
        label = (
            "min-width (retired)"
            if selection_mode == DAM_SITE_SELECTION_MIN_WIDTH
            else f"ratio h**{height_exponent}/w"
        )
    return {
        "label": label,
        "selection_mode": selection_mode,
        "height_exponent": height_exponent,
        "compartments": compartments,
        "seeds": seeds,
        "by_seed": {tuple(zone["seed"]["rowcol"]): zone for zone in compartments},
    }


def summarize_dam_site_objective(dem: dict, result: dict) -> str:
    """THE OBJECTIVE TABLE: which station each rule picks, per seed.

    Three columns, all on one parcel with everything but the objective
    held identical:
      MIN-WIDTH   the retired rule -- narrowest station wins, height
                  unconsidered. Reachable only through the escape hatch
                  kept for this table.
      h/w         the linear trade.
      h**2/w      storage grows faster than linearly with depth.

    THE EXPONENT IS CHOSEN FROM THIS TABLE, not from taste, and the
    table keeps printing so the choice stays re-measurable. The two
    exponents differ only in how far a station must be TALLER to justify
    being WIDER -- A beats B under h/w when hA/hB > wA/wB and under
    h**2/w when (hA/hB)**2 > wA/wB -- so they disagree exactly on
    taller-and-wider stations, and the disagreement count below is the
    number that matters."""
    lines = [
        "=== DAM-SITE OBJECTIVE: WIDTH ALONE vs WIDTH AND HEIGHT ===",
        "  The embankment cell used to be the MINIMUM-WIDTH station. A declared crest only",
        "  certifies that a local high point EXISTS -- it may stand 5 cm above the channel or 5 m --",
        "  so a narrow spot on a flat scored like a dam site. The objective is now",
        "  h**exponent / w: impoundment per unit of wall, with h the BINDING (lower) shoulder.",
        "",
        f"  SHIPPED: h**{DAM_SITE_HEIGHT_EXPONENT}/w. The columns below are what each rule picks.",
        "  Stations with an ABSENT binding shoulder are SKIPPED, never scored 0 -- they were never",
        "  measured for depth and must not lose on merit they were never measured for.",
        "",
    ]
    columns = [
        run_objective(dem, result, DAM_SITE_SELECTION_MIN_WIDTH),
        run_objective(dem, result, DAM_SITE_SELECTION_RATIO, DAM_SITE_HEIGHT_EXPONENT_LINEAR),
        run_objective(dem, result, DAM_SITE_SELECTION_RATIO, DAM_SITE_HEIGHT_EXPONENT_STORAGE),
    ]
    retired, linear, storage = columns
    seeds = sorted({seed for column in columns for seed in column["by_seed"]})
    if not seeds:
        lines.append("  (no seed built a compartment under any objective)")
        return "\n".join(lines)

    exponent_disagreements = []
    relocations = []
    for seed in seeds:
        lines.append(f"  SEED {seed}:")
        for column in columns:
            zone = column["by_seed"].get(seed)
            if zone is None:
                lines.append(
                    f"    {column['label']:>20}: no compartment "
                    "(this objective found no defensible dam site for this seed)"
                )
                continue
            lines.append(
                f"    {column['label']:>20}: pinch {tuple(zone['pinch']['rowcol'])} "
                f"w {zone['pinch']['width_m']:.1f} m, binding h "
                f"{_depth_cell(zone['pinch'].get('binding_height_m'))}, catchment "
                f"{zone['pinch_catchment_acres']:.2f} ac, band "
                f"{zone['compartment_footprint_acres']:.4f} ac, hull "
                f"{zone['zone_acres']:.4f} ac"
            )
        _retired_zone = retired["by_seed"].get(seed)
        _linear_zone = linear["by_seed"].get(seed)
        _storage_zone = storage["by_seed"].get(seed)
        if _linear_zone is not None and _storage_zone is not None:
            if tuple(_linear_zone["pinch"]["rowcol"]) != tuple(_storage_zone["pinch"]["rowcol"]):
                exponent_disagreements.append(
                    (seed, tuple(_linear_zone["pinch"]["rowcol"]),
                     tuple(_storage_zone["pinch"]["rowcol"]))
                )
                lines.append(
                    f"      EXPONENTS DISAGREE: h/w picks "
                    f"{tuple(_linear_zone['pinch']['rowcol'])} "
                    f"(w {_linear_zone['pinch']['width_m']:.1f}, h "
                    f"{_depth_cell(_linear_zone['pinch'].get('binding_height_m'))}), "
                    f"h**2/w picks {tuple(_storage_zone['pinch']['rowcol'])} "
                    f"(w {_storage_zone['pinch']['width_m']:.1f}, h "
                    f"{_depth_cell(_storage_zone['pinch'].get('binding_height_m'))})"
                )
        shipped_zone = storage if DAM_SITE_HEIGHT_EXPONENT == 2 else linear
        shipped_zone = shipped_zone["by_seed"].get(seed)
        if _retired_zone is None and shipped_zone is not None:
            relocations.append((seed, None, tuple(shipped_zone["pinch"]["rowcol"])))
            lines.append(
                f"      SEED GAINS A COMPARTMENT under the objective "
                f"({shipped_zone['zone_acres']:.4f} ac hull)"
            )
        elif _retired_zone is not None and shipped_zone is None:
            relocations.append((seed, tuple(_retired_zone["pinch"]["rowcol"]), None))
            lines.append(
                f"      SEED LOSES ITS COMPARTMENT under the objective -- the retired rule put a "
                f"dam at {tuple(_retired_zone['pinch']['rowcol'])} on a station the objective "
                "cannot defend"
            )
        elif _retired_zone is not None and shipped_zone is not None:
            _before = tuple(_retired_zone["pinch"]["rowcol"])
            _after = tuple(shipped_zone["pinch"]["rowcol"])
            if _before != _after:
                relocations.append((seed, _before, _after))
                lines.append(
                    f"      DAM CELL RELOCATES {_before} -> {_after}: width "
                    f"{_retired_zone['pinch']['width_m']:.1f} -> "
                    f"{shipped_zone['pinch']['width_m']:.1f} m, binding height "
                    f"{_depth_cell(_retired_zone['pinch'].get('binding_height_m'))} -> "
                    f"{_depth_cell(shipped_zone['pinch'].get('binding_height_m'))}, hull "
                    f"{_retired_zone['zone_acres']:.4f} -> {shipped_zone['zone_acres']:.4f} ac"
                )
        lines.append("")

    lines.append(
        f"  EXPONENT VERDICT: h/w and h**2/w disagree on {len(exponent_disagreements)} of "
        f"{len(seeds)} seed(s)."
    )
    if not exponent_disagreements:
        lines.append(
            "    The two are INDISTINGUISHABLE on this parcel, so the shipped exponent cannot be "
            "argued from these numbers alone -- it rests on the physics (storage grows faster than "
            "linearly with depth) and stays re-measurable here."
        )
    else:
        for seed, linear_pick, storage_pick in exponent_disagreements:
            lines.append(f"    seed {seed}: h/w -> {linear_pick}, h**2/w -> {storage_pick}")
    lines.append(
        f"  RELOCATIONS: the objective moved, gained or lost a dam cell on {len(relocations)} of "
        f"{len(seeds)} seed(s)."
    )

    # --- the skipped population, which the failure counts do not show ---
    lines.append("")
    for column in columns:
        _unscoreable = sum(
            station["dam_site_score"] is None
            for zone in column["compartments"]
            for station in zone["walk_stations"]
        )
        _total = sum(len(zone["walk_stations"]) for zone in column["compartments"])
        _failures = {}
        for record in column["seeds"]:
            # A seed can fail without a reason_code on the walk (the
            # compartment-empty guard patches its own), so the key is
            # read defensively rather than assumed -- an instrument that
            # crashes on one odd record reports nothing at all.
            _reason = record.get("reason_code", "unattributed")
            if record.get("status") == "failed":
                _failures[_reason] = _failures.get(_reason, 0) + 1
        lines.append(
            f"    {column['label']:>20}: {len(column['compartments'])} compartment(s); "
            f"{_unscoreable} of {_total} walked stations unscoreable (absent shoulder or zero "
            f"width); seed failures {_failures or '{}'}"
        )
    return "\n".join(lines)


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


# The sensitivity ladder. DIAGNOSTIC-ONLY: the shipped gate, a midpoint,
# and the level-pool arc's own measuring stick. The line reports how many
# compartments clear each; it chooses nothing, the same curve-then-choose
# discipline the half-width sweep used.
SHOULDER_SENSITIVITY_METERS = (MIN_BINDING_SHOULDER_METERS, 1.5, POOL_REFERENCE_HEIGHT_METERS)


def summarize_shoulder_gate(result: dict) -> str:
    """THE ENCLOSURE GATE'S REPORT: what every selected dam site on this
    parcel actually holds, against the bar it is held to.

    THE DISTRIBUTION IS THE POINT, not the pass/fail count. A reader
    needs to see how far this land sits from being able to impound --
    whether the refusals missed by centimetres or by metres -- because
    that is the difference between a threshold worth revisiting and a
    parcel with no embankment sites on it. So every compartment's
    binding shoulder is listed, survivors and refusals together, sorted.

    Reads the compute core's own return: refused compartments keep their
    full record on dropped_zones, which is what makes this reportable at
    all."""
    surviving = list(result["zones_by_type"][SURVEY_TYPE_EMBANKMENT])
    refused = [
        zone
        for zone in result["dropped_zones"]
        if zone["survey_type"] == SURVEY_TYPE_EMBANKMENT
        and zone["drop_reason"] == REASON_SHOULDER_BELOW_MINIMUM
    ]
    other_drops = [
        zone
        for zone in result["dropped_zones"]
        if zone["survey_type"] == SURVEY_TYPE_EMBANKMENT
        and zone["drop_reason"] != REASON_SHOULDER_BELOW_MINIMUM
    ]
    lines = [
        "=== THE ENCLOSURE GATE: CAN THE CHOSEN DAM SITE IMPOUND? ===",
        f"  MIN_BINDING_SHOULDER_METERS = {MIN_BINDING_SHOULDER_METERS} m -- NRCS CPS 378's 3 ft,",
        "  the smallest impoundment the practice standard recognises as an embankment pond. A site",
        "  whose BINDING (lower) shoulder stands below it cannot hold that, whatever its width or",
        "  catchment. The objective still picks the best available station; this asks whether the",
        "  best available is good enough, which is why a refusal can say what the reach offers.",
        "",
        f"  {len(surviving)} compartment(s) clear the gate, {len(refused)} refused by it, "
        f"{len(other_drops)} dropped for other reasons.",
        "",
    ]

    def _row(zone, verdict):
        height = zone["pinch_binding_height_m"]
        measured = "absent" if height is None else f"{height:.2f} m"
        return (
            f"    {verdict:>18}  zone {zone['id']:>3}  binding shoulder {measured:>8}  "
            f"pinch {tuple(zone['pinch']['rowcol'])}  w {zone['pinch']['width_m']:.1f} m  "
            f"hull {zone['zone_acres']:.4f} ac  catchment {zone['pinch_catchment_acres']:.2f} ac"
        )

    # EVERY COMPARTMENT THE RUN BUILT, not only the ones that reached the
    # output. A compartment dropped at the acreage floor or as an overlap
    # duplicate still HAS a selected dam site with a measured shoulder,
    # and the question this section answers -- how far is this land from
    # being able to impound -- is about the sites the walk found, not
    # about which of them survived later rules. Each is labelled with
    # what became of it so a reader can discount duplicates if they want.
    lines.append("  EVERY SELECTED DAM SITE THE RUN FOUND, deepest first:")
    _all = (
        [(zone, "CLEARS") for zone in surviving]
        + [(zone, "refused") for zone in refused]
        + [(zone, str(zone["drop_reason"])[:18]) for zone in other_drops]
    )
    if not _all:
        lines.append("    (no compartment was built on this parcel at all)")
    for zone, verdict in sorted(
        _all, key=lambda entry: -(entry[0]["pinch_binding_height_m"] or -1.0)
    ):
        lines.append(_row(zone, verdict))
    if _all:
        lines.append(
            f"    ({len(surviving)} in the output, {len(refused)} refused by this gate, "
            f"{len(other_drops)} dropped by a later rule -- every one a real chosen site)"
        )

    # --- THE DISTRIBUTION, which is the number that matters ---
    _heights = [
        zone["pinch_binding_height_m"]
        for zone, _ in _all
        if zone["pinch_binding_height_m"] is not None
    ]
    lines.append("")
    if _heights:
        _heights_sorted = sorted(_heights)
        _median = _heights_sorted[len(_heights_sorted) // 2]
        lines.append(
            f"  DISTRIBUTION across {len(_heights)} selected dam site(s): "
            f"min {min(_heights):.2f} m, median {_median:.2f} m, max {max(_heights):.2f} m "
            f"(gate {MIN_BINDING_SHOULDER_METERS} m)."
        )
        lines.append("    " + " ".join(f"{height:.2f}" for height in _heights_sorted))
        if max(_heights) < MIN_BINDING_SHOULDER_METERS:
            lines.append(
                "    NOT ONE SITE ON THIS PARCEL CLEARS THE GATE. That is a finding about the land, "
                "not a defect in the threshold -- the best dam site the whole run could find cannot "
                f"hold {MIN_BINDING_SHOULDER_METERS} m."
            )
    else:
        lines.append("  DISTRIBUTION: no selected dam site carries a measured binding shoulder.")

    # --- THE SENSITIVITY LADDER: how many survive at each bar ---
    lines.append("")
    lines.append("  SENSITIVITY -- compartments clearing each candidate bar (CHOOSES NOTHING):")
    for bar in SHOULDER_SENSITIVITY_METERS:
        clearing = sum(1 for height in _heights if height >= bar)
        note = ""
        if bar == MIN_BINDING_SHOULDER_METERS:
            note = "   <- SHIPPED (CPS 378's 3 ft, the permissive end of the bracket)"
        elif bar == POOL_REFERENCE_HEIGHT_METERS:
            note = "   <- valley_level_pool's POOL_REFERENCE_HEIGHT_METERS, the measuring stick"
        lines.append(
            f"    {bar:>5.2f} m: {clearing} of {len(_heights)} selected site(s) clear it{note}"
        )
    lines.append(
        "    The ladder is printed so the gate's placement stays re-measurable from evidence, the "
        "same discipline the half-width sweep used. Nothing here is chosen by this line."
    )
    return "\n".join(lines)


def summarize_bound_outcome_shift(retired_run: dict, shipped_run: dict) -> str:
    """What the half-width bound did to the OUTPUT, not just to the
    measurements: the survivor set, the presented set and the pooled
    selection, at the retired bound against the shipped one.

    WHY THIS NEEDS TWO FULL RUNS and cannot be read off the compartment
    table above. Everything between "a compartment exists" and "a
    compartment ships" happens in the compute core -- the catchment
    ceiling, compartment-overlap dedupe, the acreage floor, per-type
    ranking, the presentation rule and the pooled selection -- and
    re-deriving any of it here would make this report a second
    implementation of the thing it is checking. So both arguments are
    real identify_water_survey_areas() returns, run over one parcel with
    one DEM and one set of production areas, differing in the bound and
    in nothing else.

    REPORTED, NOT ASSERTED. A longer walk changes widths, so the
    profile's minimum changes, so the dam cell changes -- the selection
    moving is a legitimate consequence and this says whether it did."""
    lines = ["  OUTCOME SHIFT (survivors / presented / pooled selection):"]
    for label, run in (("retired bound", retired_run), ("shipped bound", shipped_run)):
        result = run["result"]
        counts = {
            survey_type: len(result["zones_by_type"][survey_type])
            for survey_type in result["zones_by_type"]
        }
        presented = [
            (zone["survey_type"], zone["rank"])
            for zone in sorted(
                (z for z in result["zones"] if z["presented"]),
                key=lambda z: z["presentation_order"],
            )
        ]
        selected = result["selected_water_zone"]
        lines.append(
            f"    {label:>14}: survivors {counts}, presented {presented}, "
            f"rule '{result['presentation']['rule_applied']}'"
        )
        lines.append(
            f"                    selection "
            + (
                "None"
                if selected is None
                else f"{selected['survey_type']} rank {selected['rank']}, "
                f"mean {selected['mean_suitability']:.4f}, "
                f"{selected['zone_acres']:.4f} ac"
            )
        )
    retired_selected = retired_run["result"]["selected_water_zone"]
    shipped_selected = shipped_run["result"]["selected_water_zone"]

    def _identity(zone):
        # NOT the zone id: ids are assigned per run over the full
        # cross-type list, so a compartment appearing or disappearing
        # renumbers everything after it and two runs' ids are not
        # comparable. Type plus geometry is.
        if zone is None:
            return None
        return (zone["survey_type"], round(zone["polygon_utm"].area, 3))

    if _identity(retired_selected) == _identity(shipped_selected):
        lines.append(
            "    selected_water_zone: UNCHANGED (same type, same polygon area) -- the bound moved "
            "measurements without moving the pooled winner."
        )
    else:
        lines.append(
            f"    selected_water_zone: MOVED -- {_identity(retired_selected)} -> "
            f"{_identity(shipped_selected)}. A longer walk changes widths, so the profile minimum "
            "changes, so the dam cell changes; this is that consequence reaching the output."
        )
    return "\n".join(lines)


def summarize_pinch_bearing_and_bound(dem: dict, result: dict) -> str:
    """The whole instrument as terminal text.

    `result` is the compute core's own return, which carries every array,
    mask, surface and polygon the embankment pass ran on."""
    lines = [
        "=== PINCH BEARING + HALF-WIDTH BOUND: THREE-WAY ATTRIBUTION ===",
        f"  1  D8 + {RETIRED_HALF_WIDTH_BOUND_METERS:.0f} m      the record before either change",
        f"  2  secant + {RETIRED_HALF_WIDTH_BOUND_METERS:.0f} m  the BEARING alone      (2 - 1)",
        f"  3  secant + {RIDGE_WALK_MAX_HALF_WIDTH_METERS:.0f} m  PRODUCTION TODAY: the BOUND on "
        f"top of it   (3 - 2)",
        "  4+ the rest of the sweep, so the curve that chose the shipped bound keeps printing.",
        "",
        "  Every configuration runs on this run's OWN arrays, masks, surfaces and polygons --",
        "  nothing is rebuilt, which is the whole validity of the comparison.",
        "",
        "  BOTH CHANGES HAVE NOW SHIPPED. The D8 column reaches the retired bearing through the",
        f"  escape hatch kept for this table; the {RETIRED_HALF_WIDTH_BOUND_METERS:.0f} m columns "
        "reach the retired bound the same way.",
        "  A LONGER BOUND IS NOT JUST MISSING CRESTS FILLED IN: measured widths change, so the",
        "  profile's minimum changes, so the DAM CELL changes -- and catchments, baselines,",
        "  transects and drawn zones follow it. That is what the relocation column is for.",
        "",
    ]

    # THE FOUR COLUMNS, named explicitly rather than derived by excluding
    # whatever happens to be shipped -- so that changing the shipped
    # constant moves which column is labelled PRODUCTION without silently
    # changing what any column MEANS.
    baseline = run_configuration(
        dem, result, PINCH_BEARING_D8, RETIRED_HALF_WIDTH_BOUND_METERS
    )
    bearing_only = run_configuration(
        dem, result, PINCH_BEARING_SECANT, RETIRED_HALF_WIDTH_BOUND_METERS
    )
    production = run_configuration(
        dem, result, PINCH_BEARING_SECANT, RIDGE_WALK_MAX_HALF_WIDTH_METERS
    )
    beyond = [
        run_configuration(dem, result, PINCH_BEARING_SECANT, bound)
        for bound in HALF_WIDTH_SWEEP_METERS
        if bound not in (RETIRED_HALF_WIDTH_BOUND_METERS, RIDGE_WALK_MAX_HALF_WIDTH_METERS)
    ]
    configurations = [baseline, bearing_only, production] + beyond
    swept = [production] + beyond

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
        # GAINED OR LOST A COMPARTMENT is its own line, because a seed
        # that finds no constriction at one bound and builds at another
        # is not a delta on any number -- it is a zone appearing or
        # disappearing, and a table of signed differences would show it
        # as two blanks.
        _production_zone = production["by_seed"].get(seed)
        if middle is None and _production_zone is not None:
            lines.append(
                f"      BOUND  (3-2): SEED GAINS A COMPARTMENT -- no constriction at "
                f"{RETIRED_HALF_WIDTH_BOUND_METERS:.0f} m, builds at "
                f"{RIDGE_WALK_MAX_HALF_WIDTH_METERS:.0f} m "
                f"(pinch {tuple(_production_zone['pinch']['rowcol'])}, "
                f"{_production_zone['zone_acres']:.4f} ac hull)"
            )
        elif middle is not None and _production_zone is None:
            lines.append(
                f"      BOUND  (3-2): SEED LOSES ITS COMPARTMENT -- built at "
                f"{RETIRED_HALF_WIDTH_BOUND_METERS:.0f} m "
                f"(pinch {tuple(middle['pinch']['rowcol'])}), none at "
                f"{RIDGE_WALK_MAX_HALF_WIDTH_METERS:.0f} m"
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
                "    The bound is not binding on this run, so the curve has nothing to say about "
                f"whether the shipped {RIDGE_WALK_MAX_HALF_WIDTH_METERS:.0f} m is right for THIS "
                "parcel -- it was chosen on another."
            )
        elif last_bound_absences == 0:
            lines.append(
                "    Every bound-stopped flank resolves by the last swept value, so on this "
                f"parcel the shipped {RIDGE_WALK_MAX_HALF_WIDTH_METERS:.0f} m is still leaving "
                "measurement on the table. Whether that is worth another change is a decision for "
                "the curve, not for this line."
            )
        elif last_bound_absences >= first_bound_absences * 0.5:
            lines.append(
                "    Absences PERSIST as the bound grows: these flanks are unbounded hillside "
                "rather than a shoulder just out of reach, and extending the bound further is not "
                "the answer -- the crest concept itself would need rethinking."
            )
        else:
            lines.append(
                "    Absences fall off but do not vanish: part of the tally is a bound that is "
                "still short and part is genuinely unbounded ground. The split is above -- this is "
                f"the shape that chose the shipped {RIDGE_WALK_MAX_HALF_WIDTH_METERS:.0f} m on the "
                "reference property, at its knee rather than its floor."
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
