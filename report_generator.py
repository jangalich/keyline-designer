"""
report_generator.py

Takes the output of soil_data.py, elevation_data.py, hydrology_data.py,
climate_data.py, and imagery_data.py and generates a narrative Scale of
Permanence report using the Claude API.

This is where the "AI" part of the tool actually earns its keep — not by
inventing a design out of nothing, but by reasoning across multiple real
data layers and explaining tradeoffs in plain language, the way a human
permaculture/keyline consultant would after reviewing a site.

Requires an Anthropic API key set as an environment variable:
    export ANTHROPIC_API_KEY="sk-ant-..."

Docs: https://docs.claude.com/en/api/overview
"""

import os
from typing import Optional
from anthropic import Anthropic

MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """You are assisting with a Scale of Permanence analysis for a small
regenerative farm property, following the framework popularized by P.A. Yeomans and
taught in modern regenerative agriculture (e.g. Richard Perkins' work). The Scale of
Permanence orders design decisions from least changeable to most changeable, and a
sound analysis reasons through the factors IN THAT ORDER, because the conclusion of
each step constrains the ones that follow it.

You will be given real climate and geospatial data for a specific property: historical
climate data (prevailing wind, rainfall, temperature), soil survey data, an elevation
grid, nearby surface water features, and a satellite-derived land cover snapshot
(NDVI-based: percent bare/degraded ground, low vegetation, high-vigor vegetation, and
open water).

REASONING SEQUENCE — follow this exact order, do not skip ahead or reorder it:

1. CLIMATE. Start here because it is the least changeable factor and frames everything
   after it. Summarize prevailing wind direction and rainfall volume/intensity from the
   climate data, and state directly what each implies for later steps (wind direction
   constrains windbreak orientation in step 5; rainfall intensity constrains pond/swale
   sizing in step 3). Temperature is useful context — mention it briefly — but is more
   relevant to future crop/species selection than to the land-design decisions below,
   so don't over-invest in reasoning about it.

2. LAND SHAPE (topography, keyline, swales). Using the elevation grid, describe slope,
   aspect, and relief, and reason about where water and keyline points naturally fall.
   Identify, even loosely, which parts of the property look like strong, workable
   production land versus which are steep, awkward, or otherwise marginal — name these
   zones (e.g. "the western slope," "the low ground near the stream") so later steps can
   refer back to them. Every factor from step 3 onward must be checked against the
   zones you identify here, and none of them should casually consume land this step
   flags as strong production land.

3. WATER SUPPLY (ponds, dams, ram pumps). Combine the water-features data with Land
   Shape's slope/relief findings and Climate's rainfall-intensity finding to reason
   about where water could realistically be captured, stored, or moved (pond/dam
   siting, ram pump feasibility where elevation drop exists). State whether any
   candidate site would sit inside a zone step 2 flagged as strong production land, and
   if so, say so as a tradeoff rather than silently recommending it. When valley-based
   WATER SYSTEM CANDIDATE ZONE data is provided below (DEM/LiDAR-derived valley segments
   sitting above a candidate production area by a minimum gravity gradient), treat it as
   the strongest available signal for WHERE gravity-fed infrastructure could go, and
   describe the general zone(s) it identifies — but do not present any single point
   within a zone as a definitive pond/dam site; that requires separate, more detailed
   analysis (storage volume, dam wall geometry) this pipeline doesn't perform.

4. FARM ROADS. When ROAD NETWORK data is provided below (a single road NETWORK grown
   outward from the property's real, chosen access point by a coverage-greedy router,
   hard-screened against the single selected water-system zone and grade, with
   floodplain/hydric ground and production land as soft cost penalties — see that data's
   own notes for exactly how it stopped), narrate FROM that network. This is ONE network,
   not a set of ranked candidate routes to compare or name/rank — describe the trunk
   branch as the recommended road (its own length/grade/newly-served acreage), and state
   the network's total length and total served acreage plainly. A short spur off the
   trunk is real geometry, not a separate corridor — mention it in proportion to its own
   numbers (a few-foot stub genuinely is a minor detail, not "additional access"); do not
   inflate it into language implying it opens up new access on its own. When a spur
   serves the water-system zone sited in step 3 rather than production ground, say so
   plainly — that is what it is for. State the router's own stopping reason honestly,
   using the specific sentence supplied with the data (further road not justified by
   remaining acreage, all identified ground reached, no production area to serve, or
   ground identified but unreachable given terrain/exclusions) — do not soften or omit
   it. If no network data is available for this property (or the router found nothing at
   all to build), fall back to describing routing that would follow the ridge or contour
   lines from step 2 and avoid the water infrastructure/catchments from step 3, and say
   plainly that this is an unverified topographic suggestion, not a placement backed by
   computed network geometry. A branch MAY cross a production zone from step 2 —
   properties.crosses_production_zone reports this, and it's a real, valid routing option
   (a road is a thin linear feature, not a large permanent land claim), not something to
   flag as a problem; only note it as a genuine tradeoff (interrupted field
   access/operations) where it's actually material, not as a blanket caveat on every
   crossing branch. Note that no surveyed parcel or easement data feeds this step
   regardless of which path was used. Soil (including erosion-prone SSURGO K-factor
   ground) is deliberately NOT considered here at all — it belongs to step 8, below this
   one in the Scale of Permanence ordering. Trees, structures, and subdivision fencing
   (steps 5-7) are decided AFTER this step and must not be described as having
   constrained this network's routing — if a genuine spatial relationship between the
   road and a later-step feature is worth naming, save it for that later step's own
   section, which already carries the constraint forward from here.

5. TREES (windbreaks, riparian buffers). Use Climate's prevailing wind (step 1) for
   windbreak orientation and Water Supply's stream/pond locations (step 3) for riparian
   buffer needs. Check placement against the production zones (step 2) and road
   corridors (step 4) so tree lines reinforce rather than block them. Be explicit that
   the NDVI "high vigor vegetation" reading cannot confirm existing tree canopy (see the
   imagery note below) — treat any tree-placement recommendation here as a new proposal,
   not a validation of vegetation already on site.

6. PERMANENT BUILDINGS. Recommend where structures could plausibly go, and just as
   important, rule out any zone already claimed by earlier steps: production land from
   step 2, water-storage or drainage areas from step 3, and road or tree corridors from
   steps 4-5. State clearly that no building-code, setback, or utility-access data feeds
   this step — it is a land-suitability read only, not a permitting-ready siting. When
   SOLAR STRUCTURE CANDIDATE data is provided below (DEM-derived, ranked candidate SITES
   for a small, fixed-footprint structure — a barn or shed with rooftop panels, not a
   ground-mounted array — already screened for slope, aspect, shading, and proximity to a
   mapped road), use it as the concrete basis for any solar siting discussion: compare the
   ranked candidates against each other by name/rank rather than inventing an unranked
   one. A candidate MAY sit inside or right at the edge of a production zone —
   properties.production_zone_relationship reports this, and proximity to a production
   zone's edge is scored as a real preference (a small structure can coexist with
   production land), not something to apologize for or treat as a conflict; only flag it
   as a tradeoff if the candidate ALSO carries a prime-farmland conflict (solar value vs.
   agricultural value of that specific land) — present that explicitly rather than
   silently picking a side. Do not present any single candidate as a forced final answer
   when multiple are close in score — say so, and let the ranked list stand as real
   options.

7. SUBDIVISION FENCES. When STREAM EXCLUSION / PERIMETER FENCING data is provided below
   (fencing.py: buffered NHD stream geometry for livestock-exclusion fencing, and the
   property boundary itself for perimeter fencing), lead with those two as REAL COMPUTED
   GEOMETRY — reference them directly (by label/source_feature_id), state the stream
   exclusion buffer distance used, and don't re-derive or second-guess their geometry.
   Perimeter fencing is geometry only — do NOT recommend a fence type, height, or
   material for it; that is explicitly out of scope.

   Everything else in this section has NO computed geometry behind it and must be
   framed as narrative-only guidance, explicitly:

   - POND/WATER ZONE EXCLUSION FENCING: if a WATER SYSTEM CANDIDATE ZONE was identified
     in step 3, note that once a pond/dam is actually sited within it (a future
     capability this tool doesn't yet perform), exclusion fencing around it would
     follow — but the candidate zone itself is a band, not a sited feature, and is too
     imprecise to responsibly draw a fence line around today. Frame this as a future
     consideration, not a current recommendation, and don't describe specific fence
     geometry around the candidate zone.

   - TREE CROP/WINDBREAK EXCLUSION FENCING: same treatment — if step 5 proposed a
     windbreak or tree line, note that exclusion fencing around it would make sense
     once that planting is actually placed on the ground, but not before.

   - SUBDIVISION/ROTATIONAL FENCING: reason in prose about where it would logically
     run, referencing the ridge/valley delineation, production zones, and other
     structured context from steps 2-6 by name (e.g. "following the ridge separating
     the western and eastern production zones"). This MUST be explicitly conditional —
     do not assume livestock are part of the operation. Use framing like "if livestock
     are part of your operation, subdivision fencing would logically follow [feature] —
     but paddock sizing and layout depend on herd type and stocking rate, which this
     report doesn't currently account for." Do NOT generate specific paddock sizes,
     paddock counts, or rotation schedules.

   No legal parcel or ownership-boundary data feeds this step, for any of the above.

8. SOIL — reasoned about last, and treated as the most changeable and most improvable
   factor, not as a reason to exclude land already zoned in steps 2-7. Bring in the
   SSURGO soil data here, cross-referenced against the NDVI imagery per the imagery note
   below. Frame the section around how soil fertility and drainage can be built and
   managed WITHIN the zones and infrastructure already decided — cover cropping,
   drainage work, organic matter, animal impact — rather than revisiting or vetoing
   where production land, water, roads, trees, buildings, or fences were placed above.

Before each section from step 2 onward, open with a short sentence naming the specific
constraint(s) it inherits from the prior steps, then give that section's own findings
and recommendations. This carry-forward reasoning must be visible in the output, not
just implicit in your internal thinking.

Note on imagery/land cover data specifically: NDVI-based land cover findings are a
snapshot from a single satellite pass and must always be cross-referenced against the
SSURGO soil drainage data rather than read on their own — the same "bare ground"
reading means very different things depending on what's underneath it. Bare or
degraded patches sitting on poorly-drained soil map units suggest seasonal
waterlogging, compaction, or ponding that's suppressing growth; the same bare
patches over well-drained soil more likely point to erosion, overgrazing, or simply
disturbed/exposed subsoil. Don't diagnose a cause from the imagery alone — use the
soil data to narrow down which explanation fits, and flag it as a hypothesis worth
walking the ground to confirm, not a certainty. Also note how current the scene is
(days since capture) — a reading from many months ago is a weaker basis for
conclusions than a recent one, especially outside the growing season.

Critically, the "high vigor vegetation" bucket in this data is an NDVI reading only —
NDVI measures photosynthetic activity, not vegetation type or height, and cannot tell
a lush hayfield or thick pasture apart from mature tree canopy. Do NOT assert or imply
that this bucket represents forest, woodland, or tree cover — a property that is
entirely open, actively-grazed or hayed farmland can and does score high in this
bucket during peak growing season. If the report needs to say anything about the
presence of woody/forest cover specifically, note explicitly that this dataset can't
establish that, and that ground-truthing (a site visit) or higher-resolution/multi-
season imagery would be needed to distinguish vigorous open pasture from tree canopy.

DATA HONESTY: Farm Roads has a real, routed road NETWORK (step 4) when the router
produced one; Permanent Buildings has real candidate-zone geometry for
solar siting specifically (step 6), but nothing else about building placement;
Subdivision Fences has real computed geometry for STREAM EXCLUSION and PERIMETER
fencing specifically (step 7), but nothing else in that section — pond/water exclusion,
tree crop/windbreak exclusion, and subdivision/rotational fencing are all narrative-only
there, reasoned from structured context established in earlier steps rather than their
own computed geometry (see step 7's guidance above for exactly how to frame each). When
reasoning about parts of these sections that AREN'T backed by real candidate geometry
(non-solar building siting, Farm Roads when no network data was available, or
any Subdivision Fences content besides stream exclusion/perimeter), say plainly that no
dedicated infrastructure/parcel/zoning data exists there, rather than inventing a
specific-sounding recommendation the data can't support (an exact building footprint, a
precise fence-post count, a named legal easement, a specific paddock count). This is a
first-pass analysis from public data, not a substitute for walking the land or a
professional site visit.

OUTPUT STRUCTURE: Write the report as eight sections, in exactly this order, each under
its own header: Climate, Land Shape, Water Supply, Farm Roads, Trees, Permanent
Buildings, Subdivision Fences, Soil. Write in clear, direct prose within each section.
Avoid hedging on every sentence, but do flag genuine uncertainty where the data is thin
or ambiguous."""


def _format_soil_summary(soil_components: list[dict]) -> str:
    if not soil_components:
        return "No soil survey data available."

    lines = []
    for comp in soil_components:
        lines.append(
            f"- {comp.get('muname', 'Unknown')}: {comp.get('comppct_r', '?')}% of "
            f"map unit, drainage: {comp.get('drainagecl', 'unknown')}, "
            f"slope: {comp.get('slope_r', 'unknown')}%"
        )
    return "\n".join(lines)


def _format_elevation_summary(elevation_grid: list[dict]) -> str:
    if not elevation_grid:
        return "No elevation data available."

    elevations = [pt["elevation"] for pt in elevation_grid]
    min_e, max_e = min(elevations), max(elevations)

    # Include a handful of raw points with coordinates so Claude can reason
    # about direction of slope, not just min/max.
    sample_lines = [
        f"  ({pt['latitude']:.5f}, {pt['longitude']:.5f}): {pt['elevation']:.1f}m"
        for pt in elevation_grid
    ]

    return (
        f"Elevation range: {min_e:.1f}m to {max_e:.1f}m (relief: {max_e - min_e:.1f}m)\n"
        f"Grid sample points (latitude, longitude): elevation:\n" + "\n".join(sample_lines)
    )


def _format_water_summary(water_features: dict) -> str:
    streams = water_features.get("streams", [])
    water_bodies = water_features.get("water_bodies", [])

    if not streams and not water_bodies:
        return "No mapped streams or standing water found near this property."

    lines = []
    if streams:
        names = sorted(set(s["name"] for s in streams))
        lines.append(f"Streams/waterways: {', '.join(names)}")
    if water_bodies:
        names = sorted(set(w["name"] for w in water_bodies))
        lines.append(f"Ponds/lakes: {', '.join(names)}")

    return "\n".join(lines)


def _format_climate_summary(climate: Optional[dict]) -> str:
    if not climate:
        return "No climate data available."

    return (
        f"Prevailing wind direction: {climate['prevailing_wind_direction']} "
        f"({climate['prevailing_wind_direction_degrees']}\u00b0)\n"
        f"Average annual precipitation: {climate['avg_annual_precipitation_mm']} mm\n"
        f"Heaviest recorded single-day rainfall (last {climate['years_analyzed']} yrs): "
        f"{climate['max_daily_precipitation_mm']} mm\n"
        f"Average high/low temperature: {climate['avg_high_temp_c']}\u00b0C / "
        f"{climate['avg_low_temp_c']}\u00b0C\n"
        f"Record high/low temperature (last {climate['years_analyzed']} yrs): "
        f"{climate['record_high_temp_c']}\u00b0C / {climate['record_low_temp_c']}\u00b0C"
    )


def _format_water_candidate_zones_summary(zones_geojson: Optional[dict]) -> str:
    """Formats water_candidate_zones.py's "water_system_candidate" layer
    (see that module for the Step 1-3 valley/gradient/setback logic behind
    it) for the report prompt. Optional, same reasoning as climate/imagery
    above — a DEM fetch failure shouldn't take down the whole report."""
    if not zones_geojson or not zones_geojson.get("features"):
        return (
            "No valley-based water system candidate zones identified "
            "(either no primary valley cleared the minimum gradient above "
            "a candidate production area, or DEM data wasn't available "
            "for this property)."
        )

    lines = [f"{len(zones_geojson['features'])} candidate zone(s) identified:"]
    for feature in zones_geojson["features"]:
        props = feature["properties"]
        lines.append(
            f"  - {props['label']}: serves production area candidate(s) "
            f"{props.get('served_production_area_ids', [])}"
        )
    lines.append(
        "\nThese are general zones (valley segments above a candidate production "
        "area's elevation by a minimum gravity gradient, outside the property "
        "boundary setback) suitable for keyline plowing patterns, pond/dam "
        "potential, or ram pump routing — NOT specific pond/dam sites, which "
        "require separate, more detailed analysis (storage volume, dam wall "
        "geometry) not performed here."
    )
    return "\n".join(lines)


def _format_keypoints_summary(keypoints: Optional[list[dict]]) -> str:
    """Formats keypoint_detection.detect_keypoints()'s per-valley keypoint
    list (the inflection in each primary valley's long profile -- see that
    module) into a plain, factual data block, the same register as every
    other _format_*_summary() here: measured values only, no narrative.

    READY-TO-WIRE, NOT YET WIRED. generate_scale_of_permanence_report()
    accepts and carries the keypoint list, and this helper turns it into
    report-prompt text, but that text is deliberately NOT yet injected into
    the LLM prompt: the reviewer has not decided the narrative wording (or
    which Scale-of-Permanence section keypoints belong in) yet, and this
    module must not invent it. This helper is the staged seam -- once the
    reviewer decides, wiring it in is a one-line addition to the
    generate_scale_of_permanence_report() data_summary. Kept a pure,
    independently-testable dict-to-string function meanwhile (see
    test_report_generator.py)."""
    if not keypoints:
        return (
            "No keypoints detected on this property (no primary valley's long "
            "profile held a qualifying steep-to-gentle inflection within the "
            "boundary margin)."
        )
    lines = [f"{len(keypoints)} keypoint(s) detected:"]
    for k in keypoints:
        location = (
            "on parcel"
            if k["on_parcel"]
            else f"~{round(k['distance_outside_boundary_m'])} m outside the boundary"
        )
        lines.append(
            f"  - Keypoint {k['id']} (valley {k['valley_id']}): {k['elevation_m']} m elevation, "
            f"{k['contributing_acres']} ac contributing catchment, slope drop "
            f"{k['slope_drop_pct']}% ({k['slope_above_pct']}% above -> {k['slope_below_pct']}% below), "
            f"{location}."
        )
    lines.append(
        "\nA keypoint is the inflection in a primary valley's long profile -- the break from the "
        "steep upper reach to the gentler lower reach (Yeomans), the classic reference point for "
        "keyline cultivation layout and for a keypoint dam. These are DEM-derived and NOT surveyed; "
        "treat each as a starting point to walk and ground-truth, not a final siting."
    )
    return "\n".join(lines)


# Mirrors road_corridors.METERS_PER_FOOT exactly (a fixed physical constant, not
# something that drifts) -- kept local rather than imported so this module stays
# decoupled from road_corridors.py the same way every other _format_*_summary()
# function here is decoupled from its own source module, consuming only an
# already-computed dict.
_METERS_PER_FOOT = 0.3048

# Mirrors road_corridors.STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT (10.0)
# exactly -- the per-CELL grade above which a route's steep section is
# called out in this narrative. Kept local rather than imported for the
# same decoupling reason _METERS_PER_FOOT above is (this module consumes
# only an already-computed network dict, never road_corridors.py itself).
_STEEP_GRADE_NOTE_THRESHOLD_PCT = 10.0

# One real sentence per road_corridors.py/road_network_router.py stop_reason value
# (see route_road_network()'s own docstring for what each means) -- deliberately a
# closed set: an unrecognized value fails loudly in _format_road_corridor_summary()
# below rather than falling through to a generic, potentially misleading sentence.
_ROAD_NETWORK_STOP_REASON_SENTENCES = {
    "cost_per_acre_exceeded": (
        "Routing stopped because further road would not be justified by the "
        "additional land it would serve — {unserved_acres} acre(s) of identified "
        "production ground remain unserved."
    ),
    "all_demand_served": "The network reaches all identified production ground.",
    "no_demand": (
        "No production area was identified on this property, so no farm road is "
        "recommended."
    ),
    "no_reachable_demand": (
        "Production ground exists on this property but cannot be reached from the "
        "access point given the terrain and exclusions in play."
    ),
}


def _format_road_corridor_summary(road_network: Optional[dict]) -> str:
    """Formats road_corridors.py's build_road_network() output -- the full,
    possibly-multi-branch road NETWORK (see that module's own module
    docstring for the coverage-greedy routing and constraint stack behind
    it), NOT the "suggested_road_corridor" GeoJSON layer -- for the report
    prompt. The raw network dict is used rather than the GeoJSON layer
    specifically because stop_reason (see below) is only ever present on
    the network dict; corridors_to_geojson() collapses an empty network
    (zero branches) to an empty FeatureCollection with nowhere to carry
    that value, and stop_reason is exactly what distinguishes "no
    production land exists" from "production land exists but is
    unreachable" from "the network already reached everything worth
    reaching" -- three very different messages a farmer needs told apart.
    Optional, same reasoning as the other DEM/network-backed layers — a
    fetch failure (or an unrecognized/outdated shape from a caller not yet
    updated to this network dict) shouldn't take down the whole report;
    step 4 of the system prompt falls back to its old prose-inference
    behavior when this is empty/unavailable."""
    if not road_network or "branches" not in road_network or "stop_reason" not in road_network:
        return (
            "No road network data available (either the DEM/NHD/SSURGO data "
            "wasn't available for this property, or this run's caller hasn't "
            "supplied the current network shape) — fall back to topographic "
            "reasoning from Land Shape (step 2) for this section, and say "
            "plainly that it isn't backed by computed network geometry."
        )

    stop_reason = road_network["stop_reason"]
    if stop_reason not in _ROAD_NETWORK_STOP_REASON_SENTENCES:
        raise ValueError(
            f"_format_road_corridor_summary() doesn't recognize road network "
            f"stop_reason {stop_reason!r} -- road_network_router.route_road_network() "
            f"must have added a new stop_reason value; add its sentence to "
            f"_ROAD_NETWORK_STOP_REASON_SENTENCES rather than let this fall through "
            f"to a generic, potentially misleading message."
        )
    stop_reason_sentence = _ROAD_NETWORK_STOP_REASON_SENTENCES[stop_reason].format(
        unserved_acres=round(road_network.get("unserved_acres", 0.0), 2)
    )

    branches = road_network["branches"]
    if not branches:
        return stop_reason_sentence

    def _length_ft(branch: dict) -> float:
        return round(branch["length_meters"] / _METERS_PER_FOOT, 1)

    def _steep_section_clause(branch: dict) -> str:
        """A steep-section clause for any branch whose steepest single CELL
        (max_grade_pct) exceeds _STEEP_GRADE_NOTE_THRESHOLD_PCT, stating the
        steep length and peak grade plainly. Gated on max_grade_pct, NOT
        avg_grade_pct -- a route can average a gentle grade and still cross a
        short steep pitch, and that pitch is exactly what this surfaces, so a
        low average must never suppress it. Returns '' for a branch with no
        steep cell (nothing is added when no branch is steep)."""
        max_grade_pct = branch.get("max_grade_pct", 0.0)
        if max_grade_pct <= _STEEP_GRADE_NOTE_THRESHOLD_PCT:
            return ""
        steep_ft = round(branch.get("steep_meters", 0.0) / _METERS_PER_FOOT, 1)
        return (
            f" This route includes {steep_ft}ft above {round(_STEEP_GRADE_NOTE_THRESHOLD_PCT)}% grade, "
            f"reaching {round(max_grade_pct, 1)}%; that section will need cut-and-fill or a switchback, "
            f"not just routine grading."
        )

    trunk = next((b for b in branches if b["branch_role"] == "trunk"), branches[0])
    lines = [
        f"Recommended road: a single route ({trunk['branch_role']}) {_length_ft(trunk)}ft long, "
        f"averaging {round(trunk['avg_grade_pct'], 1)}% grade, newly serving "
        f"{round(trunk['newly_served_acres'], 3)} acre(s) of identified production ground."
        + (" [crosses a production zone]" if trunk.get("crosses_production_zone") else "")
        + _steep_section_clause(trunk)
    ]

    branch_by_index = {b["branch_index"]: b for b in branches}
    for branch in branches:
        if branch is trunk:
            continue
        parent = branch_by_index.get(branch["joins_branch_index"])
        parent_note = (
            f"off the {parent['branch_role']}" if parent is not None else "off the network"
        )
        purpose_note = ", reaching the water zone sited in step 3" if branch["branch_role"] == "water_spur" else ""
        crossing_note = " [crosses a production zone]" if branch.get("crosses_production_zone") else ""
        lines.append(
            f"  - {_length_ft(branch)}ft spur {parent_note}{purpose_note}, "
            f"{round(branch['avg_grade_pct'], 1)}% avg grade, "
            f"{round(branch['newly_served_acres'], 3)} acre(s) newly served{crossing_note}"
            + _steep_section_clause(branch)
        )

    total_length_ft = round(road_network["total_length_meters"] / _METERS_PER_FOOT, 1)
    lines.append(
        f"\nTotal network length: {total_length_ft}ft, serving "
        f"{round(road_network['total_served_acres'], 3)} acre(s) of production ground total. "
        f"{stop_reason_sentence}"
    )
    lines.append(
        "\nThis is ONE road NETWORK grown from the property's real access point, not a set of "
        "ranked candidate routes — describe the trunk above as the recommended road; mention any "
        "spur only in proportion to its own length/acreage above (a few-foot stub is a minor "
        "detail, not a second corridor — do not call it 'additional access'), and state the total "
        "network length/served acreage and the stop-reason sentence above plainly rather than "
        "omitting or softening them. Treat a production-zone crossing as a real, valid routing "
        "option (not a caveat) unless it's a genuine material tradeoff worth naming."
    )
    return "\n".join(lines)


def _format_solar_candidate_zones_summary(zones_geojson: Optional[dict]) -> str:
    """Formats solar_suitability.py's "solar_infrastructure" layer (see
    that module for the exclusion/proximity/scoring constraint stack
    behind it) for the report prompt. Optional, same reasoning as the
    other DEM/network-backed layers above — a fetch failure shouldn't
    take down the whole report."""
    if not zones_geojson or not zones_geojson.get("features"):
        return (
            "No solar infrastructure candidate zones identified (either "
            "nothing cleared the exclusion/proximity/suitability "
            "constraint stack, or DEM/road data wasn't available for "
            "this property)."
        )

    lines = [f"{len(zones_geojson['features'])} ranked candidate zone(s) identified:"]
    for feature in zones_geojson["features"]:
        props = feature["properties"]
        conflict = ""
        if props.get("prime_farmland_conflict"):
            conflict = f" — PRIME FARMLAND CONFLICT: {props.get('prime_farmland_note', '')}"
        distance_to_road = (
            f"{props['distance_to_road_ft']}ft to nearest mapped road"
            if props.get("distance_to_road_ft") is not None
            else "distance to road unknown (no road data available)"
        )
        relationship = props.get("production_zone_relationship")
        if relationship == "inside":
            production_note = "sits INSIDE a production zone (intentional — a small structure can coexist with production land)"
        elif relationship == "adjacent":
            production_note = f"{props['distance_to_production_zone_ft']}ft from the nearest production zone's edge"
        else:
            production_note = (
                f"{props['distance_to_production_zone_ft']}ft from the nearest production zone's edge"
                if props.get("distance_to_production_zone_ft") is not None
                else "no production zones identified on this property"
            )
        lines.append(
            f"  - Rank {props['rank']} (score {props['suitability_score']}/100): "
            f"{props.get('footprint_area_acres', '?')}ac footprint, {props['avg_slope_pct']}% slope, "
            f"{props['aspect']}-facing, {distance_to_road}, {production_note}{conflict}"
        )
    lines.append(
        "\nThese are ranked CANDIDATE SITES for a small, fixed-footprint structure (not a single "
        "forced placement, and not a large ground-mounted array) — compare them against each other "
        "in the narrative rather than picking one unprompted, note prime-farmland conflicts as a "
        "real tradeoff where flagged, and note that a candidate sitting inside or near a production "
        "zone is a genuine, intentional option here, not a caveat."
    )
    return "\n".join(lines)


def _format_imagery_summary(imagery: Optional[dict]) -> str:
    if not imagery:
        return (
            "No recent low-cloud satellite imagery available for this "
            "property (Planetary Computer had no qualifying Sentinel-2 "
            "scene within the lookback window)."
        )

    return (
        f"Scene date: {imagery['scene_date']} ({imagery['days_since_scene']} days ago), "
        f"cloud cover: {imagery['cloud_cover_pct']}%\n"
        f"Bare/degraded soil: {imagery['pct_bare_or_degraded_soil']}%\n"
        f"Low vegetation (pasture/grass): {imagery['pct_low_vegetation']}%\n"
        f"High vigor vegetation (dense pasture, hayfield, or tree canopy — "
        f"NDVI cannot distinguish these): {imagery['pct_dense_vegetation']}%\n"
        f"Open water: {imagery['pct_open_water']}%\n"
        f"Average NDVI: {imagery['avg_ndvi']} (range: {imagery['ndvi_min']} to {imagery['ndvi_max']})"
    )


def generate_scale_of_permanence_report(
    soil_components: list[dict],
    elevation_grid: list[dict],
    water_features: dict,
    climate_summary: Optional[dict] = None,
    imagery_summary: Optional[dict] = None,
    water_candidate_zones_geojson: Optional[dict] = None,
    solar_candidate_zones_geojson: Optional[dict] = None,
    road_network: Optional[dict] = None,
    keypoints: Optional[list[dict]] = None,
    irradiance: Optional[dict] = None,
    parcel_acres: Optional[float] = None,
    production_areas_geojson: Optional[dict] = None,
) -> str:
    """
    Given the outputs of the data-fetching modules, generates a narrative
    Scale of Permanence report via the Claude API. climate_summary (from
    climate_data.py), imagery_summary (from imagery_data.py),
    water_candidate_zones_geojson (the "water_system_candidate" layer from
    water_candidate_zones.py), solar_candidate_zones_geojson (the
    "solar_infrastructure" layer from solar_suitability.py), road_network
    (road_corridors.build_road_network()'s own full network dict --
    road_corridor_result["road_network"], NOT road_corridor_result
    ["zones_geojson"] -- see _format_road_corridor_summary()'s own
    docstring for why the raw network dict is what this needs) are all
    optional so existing callers built before those layers existed don't
    break — but including them produces
    a meaningfully better report: climate is literally the first item in
    the Scale of Permanence framework, imagery gives Claude a current
    land-cover cross-check against the soil data, the water candidate
    zones give the WATER SUPPLY section a DEM-grounded answer to "where"
    instead of reasoning from the coarse elevation grid alone, the road
    network does the same for FARM ROADS, and the solar candidate zones do
    the same for PERMANENT BUILDINGS' solar siting discussion.

    keypoints (keypoint_detection.detect_keypoints()'s per-valley list) is
    carried through and STORED here, ready for the report, but deliberately
    NOT yet injected into the LLM prompt: keypoint detection is a new,
    KSOP-independent layer, and the reviewer has not decided the narrative
    wording (or which Scale-of-Permanence section keypoints belong in) yet,
    so writing that narration now would be inventing it. The factual data
    block is available via _format_keypoints_summary(keypoints) (unit-tested
    in test_report_generator.py); wiring it into data_summary below is a
    one-line change the reviewer makes once the wording is decided. Passing
    keypoints here changes nothing about the generated report today — it is
    a forward-compatible seam, the same additive-override discipline every
    other optional argument above follows.

    irradiance (parcel_data.ParcelData.irradiance -- get_regional_
    irradiance_baseline()'s own dict, always present, 'status' key says
    whether the numbers are real) is carried through and STORED here,
    ready for the report, but deliberately NOT yet injected into the LLM
    prompt: the reviewer has not decided the narrative wording (or which
    Scale-of-Permanence section irradiance belongs in -- almost certainly
    PERMANENT BUILDINGS, alongside the existing solar siting discussion,
    but that's a content decision for later) yet, so writing that
    narration now would be inventing it. Passing irradiance here changes
    nothing about the generated report today -- it is a forward-compatible
    seam, the same additive-override discipline keypoints already
    established.

    parcel_acres (pipeline_context.PipelineContext.parcel_acres --
    production_area_ceiling.py's own boundary_polygon_utm.area /
    SQUARE_METERS_PER_ACRE) and production_areas_geojson (the
    "production_area_candidate" FeatureCollection for the ceiling-trimmed
    scored patches the map actually draws) are carried through and STORED
    here, ready for the report, but deliberately NOT yet injected into the
    LLM prompt: the Scale of Permanence narrative rewrite that will reason
    from them is a separate branch, so wiring them into data_summary now
    would be inventing narration the reviewer hasn't decided. Passing them
    here changes nothing about the generated report today -- the same
    forward-compatible seam discipline keypoints and irradiance already
    established.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable not set. Get a key from "
            "console.anthropic.com and run: export ANTHROPIC_API_KEY='sk-ant-...'"
        )

    client = Anthropic(api_key=api_key)

    data_summary = f"""CLIMATE DATA:
{_format_climate_summary(climate_summary)}

SOIL DATA:
{_format_soil_summary(soil_components)}

ELEVATION DATA:
{_format_elevation_summary(elevation_grid)}

WATER FEATURES:
{_format_water_summary(water_features)}

SATELLITE IMAGERY / LAND COVER (NDVI-derived):
{_format_imagery_summary(imagery_summary)}

WATER SYSTEM CANDIDATE ZONES (valley-based, DEM/LiDAR-derived):
{_format_water_candidate_zones_summary(water_candidate_zones_geojson)}

ROAD NETWORK (coverage-greedy cost routing from the real access point, DEM-derived):
{_format_road_corridor_summary(road_network)}

SOLAR INFRASTRUCTURE CANDIDATE ZONES (ranked, DEM-derived):
{_format_solar_candidate_zones_summary(solar_candidate_zones_geojson)}"""

    message = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Here is the geospatial data for this property:\n\n{data_summary}\n\n"
                "Please generate a Scale of Permanence analysis.",
            }
        ],
    )

    # Response content is a list of blocks; for a plain text response like
    # this, it'll be a single text block, but we join defensively in case
    # that ever changes.
    return "".join(block.text for block in message.content if block.type == "text")


if __name__ == "__main__":
    # Test with the user's real data — plugging in previously-fetched
    # results directly rather than re-calling every API, to keep this
    # test fast and focused on just the report generation step.

    test_soil = [
        {
            "muname": "Gilpin-Upshur complex, 15 to 25 percent slopes",
            "comppct_r": 50,
            "drainagecl": "Well drained",
            "slope_r": 20,
        },
        {
            "muname": "Gilpin-Upshur complex, 15 to 25 percent slopes",
            "comppct_r": 30,
            "drainagecl": "Well drained",
            "slope_r": 20,
        },
        {
            "muname": "Gilpin-Upshur complex, 15 to 25 percent slopes",
            "comppct_r": 20,
            "drainagecl": "Moderately well drained",
            "slope_r": 20,
        },
    ]

    test_elevation = [
        {"latitude": 40.64286, "longitude": -79.98383, "elevation": 326.7},
        {"latitude": 40.64346, "longitude": -79.98383, "elevation": 332.7},
        {"latitude": 40.64407, "longitude": -79.98383, "elevation": 335.2},
        {"latitude": 40.64468, "longitude": -79.98383, "elevation": 337.2},
        {"latitude": 40.64528, "longitude": -79.98383, "elevation": 344.2},
    ]

    test_water = {
        "streams": [
            {"name": "Montour Run", "feature_code": None, "geometry": None},
        ],
        "water_bodies": [],
    }

    # Illustrative placeholder climate values for this standalone test —
    # NOT real fetched data. generate_full_report.py (updated below) calls
    # climate_data.py for real numbers; this test just exercises the
    # report-generation logic in isolation without extra API calls.
    test_climate = {
        "prevailing_wind_direction": "WSW",
        "prevailing_wind_direction_degrees": 245.0,
        "avg_annual_precipitation_mm": 1020.0,
        "max_daily_precipitation_mm": 95.0,
        "avg_high_temp_c": 16.5,
        "avg_low_temp_c": 6.0,
        "record_high_temp_c": 37.0,
        "record_low_temp_c": -22.0,
        "years_analyzed": 10,
    }

    # Same reasoning as test_climate above — illustrative placeholder
    # imagery values for this standalone test, not real fetched data.
    test_imagery = {
        "scene_date": "2026-05-14",
        "days_since_scene": 63,
        "cloud_cover_pct": 4.2,
        "pct_bare_or_degraded_soil": 12.3,
        "pct_low_vegetation": 45.6,
        "pct_dense_vegetation": 40.1,
        "pct_open_water": 2.0,
        "avg_ndvi": 0.35,
        "ndvi_min": -0.1,
        "ndvi_max": 0.82,
        "valid_pixel_count": 10234,
    }

    print("Generating Scale of Permanence report...\n")
    print("-" * 60)

    try:
        report = generate_scale_of_permanence_report(
            test_soil, test_elevation, test_water, test_climate, test_imagery
        )
        print(report)
    except RuntimeError as e:
        print(f"Setup issue: {e}")
    except Exception as e:
        print(f"Request failed: {e}")
