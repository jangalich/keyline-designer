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

SYSTEM_PROMPT = """You are a regenerative farm design consultant specializing in
whole-system layout for small agricultural properties. You are writing
the analysis that accompanies a proposed layout map for one specific
property, addressed to the person who will farm it — someone building or
expanding a farm and interested in regenerative, soil-building practice.

The analysis follows the Scale of Permanence, the land-design framework
developed by P.A. Yeomans and central to keyline design. Its organizing
insight is that the factors shaping a property differ enormously in how
changeable they are, and that a sound design works through them in order
of decreasing permanence. Climate and landform are not decided by anyone
— they are read and understood first, because everything sited afterward
depends on them. From there, water, access, trees, buildings, fencing,
and soil follow, each more adjustable than the last. Each decision
informs the ones that follow it, and the elements are designed to work
together as one system.

Your recommendations are grounded in computed geospatial analysis of this
specific property, supplied below as structured data. Report from that
data rather than inferring spatial relationships, and respect what each
block says it cannot establish.

Your analysis accompanies a full-page layout map showing the recommended
features drawn on aerial imagery. Everything you are given is drawn on
that map. Write so the two work together: the map shows where, you
explain why it was chosen, what it means for how the land gets worked,
and what to do about it.

HOW TO WRITE THIS

Each section below is a set of questions. Answer them in order, as
flowing prose under the section's header — not as a Q&A list, and never
restate a question. Some questions are answered from the data supplied;
others draw on your own knowledge of keyline design and regenerative
practice. Answer both kinds fully, but never present judgment as though
the data established it.

- Address the reader directly as "you."
- Refer to features by the map legend's own names: Keypoint Candidates,
  Production Areas, Water System Survey Area, Suggested Road Corridor,
  Tree Crop Areas, Permanent Building Site, Fencing. Where several
  features share a class, distinguish them using computed values only —
  size, elevation, position, distance. Never coin a place name and never
  assert a spatial relationship the data doesn't contain.
- Where a question asks generally about a design element, answer in a
  sentence or two and tie it to this property. These are orientation for
  the reader, not lessons.
- Never revisit or second-guess a decision an earlier section made.
- Do not restate the data blocks. Use the figures that carry your
  reasoning and leave the rest.
- Never state a numeric score. The data blocks carry scores because
  that's how the analysis ranks things; the reader isn't ranking
  anything. Translate each into a plain descriptor — higher is always
  better. Say the site has strong solar exposure and only fair shading,
  not that it scored 98.3 and 90.4.
- The same goes for the analysis's own thresholds and ceilings. Named
  cutoffs, acreage limits, and cost-per-acre rules are how the pipeline
  makes its decisions; they are not how a farmer thinks about their
  land. Give the reader the meaning instead — an oversized watershed
  prone to siltation and heavy peak flows, not a 20-acre siltation and
  peak-flow ceiling.
- Report all measurements in feet, acres, inches, and °F.
- Where an analysis found nothing, say so and explain why to the extent
  the data gives a reason. You may then recommend approaches — kinds of
  intervention suited to this property — but never place them. Placement
  requires computed geometry; judgment about approach is yours.
- The whole report must stay under 3,500 words. Most sections should
  land between 250 and 400 words. Write tightly: use the figures that
  carry your reasoning, don't restate a point for emphasis, and don't
  summarize a section at its own end.

SECTIONS — write all ten, in this order, each under its own header.
Answer every question in each.

1. Introduction
   What is the purpose of this report?
   What are keyline design and the Scale of Permanence as they relate to
   this report?
   What process was used to determine the layout?
   No data block backs this section. State plainly that this is desk
   analysis from public geospatial data, not a site visit.

2. Climate
   What key climate elements define this property and its local area?
   What are the major climate threats to be aware of?
   What should the reader carry forward while reading the rest of this
   report?

3. Landform
   Generally, how does landform influence the design of a farm?
   What is this property's elevation range and relief, and what does
   that mean for working it?
   Generally, how can keypoints be used to serve a farm?
   Which Keypoint Candidates are relevant to this report, and where are
   they on the map?
   What makes the selected Production Areas suitable for production?
   What should the reader do next with this?

   Keypoints are flags, not conclusions — nothing else on this property
   was sited with reference to one. Discuss only the keypoints that
   deserve a second look; a keypoint with a small catchment and a modest
   slope break needs no more than a passing mention, or none. A keypoint
   sitting below a water opportunity is where a keyline dam wall would
   go, and warrants that reading rather than dismissal as a downstream
   point. Where a keypoint sits above ground committed to production,
   say what it means for cultivation pattern on that ground —
   keyline-pattern tillage run off the break rather than square to the
   property line — rather than leaving that for a later section.

4. Water System Survey Area
   What is a water system in the context of this report?
   Where is the Water System Survey Area on the map?
   Why is this area conducive for a water system?
   How does it serve the farm?
   Are there other water systems worth considering for supplemental
   supply? (Judgment — no data backs this. Approaches only, never
   placement.)
   What should the reader do next with this?

   This is a survey area, not a pond footprint. The area drawn is where
   opportunity is best, not the size of any structure.

   Where the Landform section flagged a keypoint sitting below this
   survey area, carry that forward here and say what it means for
   siting a dam wall. A flag raised earlier and dropped is worse than
   one never raised.

   The supplemental-supply question is judgment, not data. Answer it
   with categories of approach — roof catchment, contour swales,
   mechanical lift that needs no grid power, and whatever else suits
   this property — not with named products.

5. Suggested Road Corridor
   Generally, what purpose does this element serve?
   How was the suggested route determined?
   How much access does it provide to the farm?
   What should the reader do next with this?

   This is one network grown from the property's real access point, not
   a set of options. Report network-level figures; do not enumerate
   every branch or list every grade. Report what the network opens up.
   Serving every last corner of production ground was never the goal,
   so don't report unserved acreage, and don't characterize where
   routing stopped — neither as a failure nor as extension available
   later. State the access the network provides and move on.

6. Tree Crop Areas
   Generally, how can trees benefit the farm?
   What types of tree crop should be considered given this farm's
   location and these areas' characteristics? (Judgment — species and
   enterprise types are yours to recommend. Placing anything beyond the
   drawn areas is not.)
   How do they integrate with the earlier KSOP elements?
   What should the reader do next with this?

   Recommending categories of tree crop is your judgment, drawn from
   these areas' slope, soil, and this climate — orchard, timber block,
   windbreak, silvopasture, and so on. The data explains why this ground
   was selected as tree ground rather than production ground; it says
   nothing about which category suits it. Don't reason from one to the
   other, and don't explain what any scoring factor does or doesn't
   imply.

7. Permanent Building Site
   What does this area indicate?
   Where is it on the map?
   What are the benefits of placing a permanent building here?
   What should the reader do next with this?

   Include a rooftop solar viability read from the irradiance data. The
   site is placed on the Suggested Road Corridor by design — do not
   report its distance to a road as a finding.

8. Fencing
   Generally, what purpose does a fence serve on a farm?
   Which areas need fencing, and why?
   What subdivision fencing approaches would suit this operation?
   What next steps should be taken?

   No computed geometry backs this section and none is supplied. Reason
   from what the earlier sections established. Make no geometric claims.

   Production Areas do not automatically need permanent perimeter
   fencing. Whether and how to divide production ground belongs in the
   subdivision fencing discussion, where flexible and temporary
   approaches are usually the better answer. Permanent fencing is worth
   recommending where there's a real reason for it.

9. Soil
   What are this property's soil characteristics?
   How can the upstream KSOP elements improve the soil?
   What soil-building practices can be integrated into this plan?
   What next steps should be taken?

   Cross-reference the soil survey against the land cover reading. Do
   not use soil to revisit where anything above was placed.

   Cultivation pattern on the Production Areas is covered in the
   Landform section. Don't repeat it here; focus on fertility,
   structure, biology, and cover.

10. Summary
    What are the key findings from this report?
    What combination of enterprises would work together here — where does
    one's output become another's input?
    Where should the reader start?

    Ground any enterprise ideas in what the sections above established.

    This is a synthesis question, not an inventory. Don't walk back
    through the features and attach a revenue idea to each; infrastructure
    like a solar array or a road is not an enterprise. Think about what
    this ground, this water, and this climate can actually carry, and how
    those pieces feed each other — what one enterprise produces that
    another can use, and where they compete for the same ground or the
    same hours.

    The Production Areas exist because that ground is suited to
    production, so any enterprise answer has to say what gets grown or
    raised there. Cultivation — market gardening, field crops, forage —
    connects to the rest of the system as directly as anything else:
    compost and manure from livestock, irrigation from a developed water
    source, wind protection from the tree areas, and it's where the
    soil-building practices in the Soil section actually get applied. An
    answer covering livestock, trees, and infrastructure but skipping
    what's cultivated has missed the property's primary land use.

    For the closing question, sequence the work by dependency rather than
    listing tasks. What has to be confirmed on the ground before anything
    is built, what can begin immediately with what's already there, and
    what waits on the answers to the first group. The reader should
    finish knowing the order and why it's that order."""


# Unit conversions applied at the formatter boundary only -- the report
# narrates imperial (feet, acres, inches, degF) while everything upstream
# stays metric. The KSOP narrative_data blocks already arrive imperial,
# converted at their own source modules; these two constants cover the raw
# layers (climate, elevation, keypoints) this module formats itself.
_FEET_PER_METER = 1.0 / 0.3048
_MM_PER_INCH = 25.4


def _celsius_to_fahrenheit(celsius: float) -> float:
    return round(celsius * 9.0 / 5.0 + 32.0, 1)


def _locative_descriptor(centroid, boundary_polygon_utm) -> str:
    """
    Cardinal descriptor for where a feature sits within the parcel --
    "northwest", "north-central", "east-central", "central", etc.: the
    parcel boundary's bounding box is split into thirds on each axis and
    the feature's centroid named by the cell it lands in. The map legend
    labels feature CLASSES, not individual features, so position is the
    only way the narrative can distinguish two features of the same
    class. Both geometries must share a projected (meters) CRS. A
    centroid slightly outside the bounding box (e.g. an off-parcel
    keypoint within the detection margin) is clamped to the nearest edge
    cell rather than rejected.
    """
    minx, miny, maxx, maxy = boundary_polygon_utm.bounds
    fraction_x = (centroid.x - minx) / (maxx - minx) if maxx > minx else 0.5
    fraction_y = (centroid.y - miny) / (maxy - miny) if maxy > miny else 0.5
    column = min(2, int(max(0.0, min(1.0, fraction_x)) * 3))
    row = min(2, int(max(0.0, min(1.0, fraction_y)) * 3))
    north_south = ("south", "", "north")[row]  # UTM +y is north
    east_west = ("west", "", "east")[column]
    if north_south and east_west:
        return north_south + east_west
    if north_south:
        return north_south + "-central"
    if east_west:
        return east_west + "-central"
    return "central"


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
    """Elevation range and relief only, in feet. The per-point coordinate
    dump this used to include (so the model could infer slope direction
    from raw grid points) is deliberately gone: production_area_ceiling's
    narrative block now carries real per-patch slope/aspect/position
    figures, so the raw dump was redundant and invited spatial inference
    the data doesn't support."""
    if not elevation_grid:
        return "No elevation data available."

    elevations = [pt["elevation"] for pt in elevation_grid]
    min_ft = round(min(elevations) * _FEET_PER_METER)
    max_ft = round(max(elevations) * _FEET_PER_METER)

    return (
        f"Elevation range: {min_ft}ft to {max_ft}ft (total relief: {max_ft - min_ft}ft) "
        f"across {len(elevation_grid)} sample points."
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
    """Imperial at this boundary (inches, degF) -- climate_data.py stays
    metric; the conversion happens here, once, at the formatter."""
    if not climate:
        return "No climate data available."

    return (
        f"Prevailing wind direction: {climate['prevailing_wind_direction']} "
        f"({climate['prevailing_wind_direction_degrees']}\u00b0)\n"
        f"Average annual precipitation: "
        f"{round(climate['avg_annual_precipitation_mm'] / _MM_PER_INCH, 1)} in\n"
        f"Heaviest recorded single-day rainfall (last {climate['years_analyzed']} yrs): "
        f"{round(climate['max_daily_precipitation_mm'] / _MM_PER_INCH, 1)} in\n"
        f"Average high/low temperature: {_celsius_to_fahrenheit(climate['avg_high_temp_c'])}\u00b0F / "
        f"{_celsius_to_fahrenheit(climate['avg_low_temp_c'])}\u00b0F\n"
        f"Record high/low temperature (last {climate['years_analyzed']} yrs): "
        f"{_celsius_to_fahrenheit(climate['record_high_temp_c'])}\u00b0F / "
        f"{_celsius_to_fahrenheit(climate['record_low_temp_c'])}\u00b0F"
    )


def _format_water_candidate_zones_summary(water_narrative: Optional[dict]) -> str:
    """Formats water_candidate_zones.py's own 'narrative_data' block (see
    build_narrative_data() there for the field contract -- pre-digested,
    FINAL, imperial values) for the report prompt. This reads ONLY that
    block -- never the raw zone geometry or GeoJSON layer it replaced
    here; every number below was already converted and rounded at the
    source module. Optional, same reasoning as climate/imagery above -- a
    DEM fetch failure shouldn't take down the whole report."""
    if not water_narrative or not water_narrative.get("zone_found"):
        return (
            "No water system candidate survey area identified (no "
            "production area existed to serve, no drainage cell cleared "
            "the eligibility gates, or DEM data wasn't available for "
            "this property)."
        )

    # EVERY candidate is listed. Generation is uncapped by design (see
    # water_candidate_zones.WATER_ACCUMULATION_SEED_BUDGET) and this block
    # is generation-side, so it reports what generation produced. Trimming
    # to a presentation top-N is a RANKING operation and belongs in the
    # scoring branch, applied to rank after water_suitability.py has scored
    # every candidate against every other -- flagged here, not done here.
    lines = [
        f"{water_narrative['candidate_count']} candidate survey area(s) identified "
        f"(from {water_narrative['nomination']['keypoints_considered']} detected keypoint(s)). Each is "
        "the ground a level pool at a fixed reference waterline would cover upstream of one anchor "
        "cell, plus the dam-axis band across the valley there. THE REFERENCE WATERLINE IS A MEASURING "
        "STICK USED TO COMPARE SITES ON EQUAL TERMS, NOT A PROPOSED DAM HEIGHT."
    ]
    for zone in water_narrative["zones"]:
        provenance = zone["provenance"]
        if provenance["nominated_by"] == "keypoint":
            # THE KEYPOINT IS THE POOL'S TAIL, NOT ITS WALL. Saying only
            # "nominated from keypoint N" would leave a reader placing the
            # structure at the keypoint marker on the map; the offset is
            # what tells them the two are different places.
            source = (
                f"nominated from keypoint {provenance['keypoint_id']} "
                f"(valley {provenance['valley_id']}) but anchored "
                f"{provenance['wall_offset_downstream_ft']} ft DOWNSTREAM of it, at the first ground a "
                "full reference height below the keypoint -- that anchor is where a wall would stand, "
                "and the keypoint is the upstream tail of the water it would hold"
            )
            if provenance["anchor_off_parcel"]:
                source += (
                    f", whose anchor sits {provenance['anchor_distance_outside_boundary_ft']} ft "
                    "OUTSIDE the drawn boundary -- the dam would stand at the property edge and the "
                    "acreage below is the ON-PARCEL portion of the pool it would impound"
                )
        else:
            source = (
                "nominated from the highest remaining flow accumulation (such an anchor is already a "
                "wall site, with no keypoint above it to be offset from)"
            )

        location = zone["location"]
        drainage = zone["drainage"]
        service = zone["service"]
        pool = zone["level_pool"]
        percentile = location["elevation_percentile_of_parcel"]
        percentile_clause = (
            f", at the {percentile} elevation percentile of the parcel (0 = the parcel's lowest ground, "
            "100 = its highest)"
            if percentile is not None
            else ""
        )
        lines.append(
            f"\nSurvey area {zone['id']}: {zone['area_acres']} acre(s), {source}, in the parcel's "
            f"{location['position_in_parcel']}{percentile_clause}."
        )
        lines.append(
            f"Drainage: median contributing area {drainage['contributing_area_acres']} acre(s) across the "
            f"zone's own cells -- every member cell sits under the "
            f"{drainage['contributing_area_ceiling_acres']}-acre siltation/peak-flow eligibility ceiling -- "
            f"with a median local slope of {drainage['slope_median_pct']}%."
        )
        lines.append(
            f"Level pool at a {pool['reference_height_ft']} ft reference waterline: dam-axis band "
            f"{pool['dam_band_width_ft']} ft wide across the valley; left abutment "
            + (
                f"{pool['abutment_distance_left_ft']} ft out"
                if pool["abutment_found_left"]
                else "NOT FOUND within the search width (the ground never rises to the waterline on that side)"
            )
            + ", right abutment "
            + (
                f"{pool['abutment_distance_right_ft']} ft out"
                if pool["abutment_found_right"]
                else "NOT FOUND within the search width"
            )
            + "."
        )
        for side in ("left", "right"):
            if pool[f"crosses_major_drainage_{side}"]:
                lines.append(
                    f"  - WARNING: the dam axis runs into a SECOND major drainage "
                    f"{pool[f'major_drainage_distance_{side}_ft']} ft to the {side} (contributing area "
                    "above the siltation/peak-flow ceiling). A wall spanning that far would dam two "
                    "creeks -- a siting problem, not simply a longer wall. This is a different finding "
                    "from an abutment that was not found."
                )
        for station in pool["stations"]:
            if station["status"] != "measured":
                # NOT dry -- unmeasured. The traced channel ended before
                # this station, so there is no cross-section to report. A
                # 0 ft width here would be terrain this survey never saw.
                lines.append(
                    f"  - cross-section {station['offset_upstream_ft']} ft upstream: NOT MEASURED -- the "
                    f"traced channel ends {station['along_stem_distance_ft']} ft upstream of the dam line, "
                    "so there is no channel here to cross-section. This is the absence of a measurement, "
                    "not a dry cross-section."
                )
                continue
            lines.append(
                f"  - cross-section {station['offset_upstream_ft']} ft upstream (bearing "
                f"{station['bearing_deg']} deg): flooded width {station['flooded_width_ft']} ft, flooded "
                f"cross-sectional area {station['flooded_cross_section_area_sqft']} sq ft (a "
                "cross-section, NOT a storage capacity -- no volume is computed anywhere in this pipeline)"
            )
        overlap = zone["overlap"]
        lines.append(
            "Reported overlaps (NOT used to shrink the zone -- water does not stop at a canopy edge): "
            f"canopy {overlap['canopy_overlap_pct']}%, roads {overlap['road_overlap_pct']}% of member cells."
        )
        if zone["flags"]:
            lines.append(f"Flags: {', '.join(zone['flags'])}.")
        lines.append(
            f"Serves {service['served_production_area_count']} production area candidate(s) "
            f"{service['served_production_area_ids']}, most gravity-favorable first:"
        )
        for rel in service["relationships"]:
            gradient_clause = (
                f"{rel['gradient_pct']}% grade"
                if rel["gradient_pct"] is not None
                else "gradient undefined -- the zone adjoins/overlaps this area, no horizontal run"
            )
            lines.append(
                f"  - production area {rel['production_area_id']}: elevation differential "
                f"{rel['elevation_differential_ft']} ft (positive = the zone sits ABOVE this area) over "
                f"{rel['distance_ft']} ft ({gradient_clause}); can_gravity_feed: {rel['can_gravity_feed']}. "
                + (
                    "A gravity-feed relationship."
                    if rel["can_gravity_feed"]
                    else "Delivering water to this area would need a pump -- a real cost/maintenance "
                    "tradeoff, not a disqualification."
                )
            )

    unproductive = [
        outcome
        for outcome in water_narrative["nomination"]["keypoint_outcomes"]
        if outcome["candidate_id"] is None
    ]
    if unproductive:
        lines.append(
            "\nKeypoints that produced no candidate, with the reason code that stopped each: "
            + "; ".join(
                f"keypoint {outcome['keypoint_id']} ({outcome['contributing_acres']} ac catchment"
                + (
                    ""
                    if outcome["on_parcel"]
                    else f", {outcome['distance_outside_boundary_ft']} ft off parcel"
                )
                + f"): {outcome['outcome']}"
                for outcome in unproductive
            )
            + "."
        )

    lines.append(
        "\nThese are general survey areas suitable for keyline plowing patterns, pond/dam potential, "
        "or ram pump routing — NOT specific pond/dam sites, which require separate, more detailed "
        "analysis (a real survey, dam wall geometry, spillway design, geotechnical work at the "
        "abutments) not performed here. No storage volume is reported anywhere, deliberately."
    )
    return "\n".join(lines)


def _seed_signature_clause(region: dict) -> str:
    """The embankment seed's per-criterion signature as prose -- the
    anchor-claim half of the compartment honesty split (its counterpart,
    the compartment's own means, is the criteria clause beside it)."""
    return ", ".join(
        f"{name} {score}" for name, score in region.get("seed_criteria_signature", {}).items()
    )


def _format_water_survey_areas_summary(water_narrative: Optional[dict]) -> str:
    """Formats water_survey_areas.build_narrative_data()'s block (the
    survey-area water step that replaced the demoted level-pool arc --
    see that module's docstring) for the report prompt. Same rules as
    every _format_*_summary() here: reads ONLY the pre-digested narrative
    block, never geometry; every number was converted and rounded at the
    source. ALL survey zones are listed (no presentation cap --
    first-run posture). The two types carry their own sentences since
    the compartment change: an EXCAVATED zone gets the DUAL ACREAGE
    sentence ("X acres to survey, anchored by Y acres of
    high-suitability ground") with per-criterion means over member
    cells; an EMBANKMENT zone is a VALLEY COMPARTMENT and gets the
    honesty-split sentence -- the SEED's blend score (the anchor claim)
    beside the compartment's own criterion means (the walked ground,
    which deliberately includes low-scoring side slopes and the wall
    reach), plus the FILL CLAIM as its own sentence -- the catchment at
    the compartment's dam reach and the drainage band scored on it,
    which is a different question from either of those two and is
    reported apart from both so a compartment can honestly read as good
    ground with no water above it. Prose may only claim what a
    criterion actually scored.
    _format_water_candidate_zones_summary() above remains for the
    demoted layer's narrative shape and is no longer called on the
    pipeline path."""
    if not water_narrative or not water_narrative.get("zone_found"):
        return (
            "No water survey areas were identified (no embankment seed produced a valley "
            "compartment and nothing cleared the excavated suitability threshold -- or DEM "
            "data wasn't available for this property)."
        )

    selection = water_narrative["selection"]
    lines = [
        f"{water_narrative['zone_count']} water SURVEY ZONE(S) identified "
        f"({water_narrative['embankment_zone_count']} embankment-type -- small dam across a "
        f"drainageway; {water_narrative['excavated_zone_count']} excavated-type -- dugout or "
        "seep-fed excavated pond; NRCS Agriculture Handbook 590's two pond types). THE TWO TYPES "
        "ARE GENERATED DIFFERENTLY: an embankment zone is a VALLEY COMPARTMENT -- a dam site at "
        "the valley's walked width minimum, the storage reach above it, ridge-bounded by the dam "
        "site's own watershed -- seeded from the highest-scoring cells of its suitability surface; "
        "an excavated zone is nearby high-suitability ground grouped into a single walkable "
        f"envelope ({water_narrative['member_region_count']} member region(s) across the excavated "
        "zones), the anchoring ground carried intact inside it. These are GENERAL AREAS WORTH "
        "SURVEYING, not designed ponds -- no pool, wall, volume, or station is computed anywhere in "
        "this step, deliberately. "
        + water_narrative["twi_note"]
    ]
    # The counts line: everything that survived is shown (the
    # presentation cap was deleted -- the user decides), plus what the
    # floor/dedupe pruned and which embankment seeds honestly produced
    # nothing, so the reader knows exactly what this section is NOT
    # showing and why.
    lines.append(
        f"All {water_narrative['zone_count']} surviving zone(s) are listed, ranked per type -- "
        "no presentation cap; you decide which to walk"
        + (
            f". {water_narrative['dropped_count']} zone(s) were dropped (under the 0.1-acre floor, "
            "or a duplicate of a better-seeded compartment) -- listed in the diagnostic export "
            "with their reasons, not planned on"
            if water_narrative["dropped_count"]
            else ""
        )
        + "."
    )
    if water_narrative.get("embankment_failed_seed_count"):
        failed = water_narrative["embankment_failed_seeds"]
        reason_clause = "; ".join(
            f"blend {entry['blend_score']}: {entry['reason_code']}" for entry in failed
        )
        lines.append(
            f"{water_narrative['embankment_failed_seed_count']} of "
            f"{water_narrative['embankment_seed_count']} embankment seed(s) produced NO compartment "
            "-- honestly, with the terminator named, because there is no fallback on the "
            f"compartment path ({reason_clause})."
        )
    if selection["selected_zone_id"] is not None:
        lines.append(
            f"Selected for downstream planning: zone {selection['selected_zone_id']} "
            f"({selection['selected_survey_type']}-type) -- the two types pooled on each type's own "
            "score (embankment by its compartment rank score -- seed blend combined with the "
            "catchment at its dam reach -- excavated by member-mean suitability; a provisional "
            "selection rule awaiting tuning against the next real run)."
        )

    for region in water_narrative["zones"]:
        criteria_clause = ", ".join(
            f"{name} {entry['mean_score']} (weight {entry['weight']})"
            for name, entry in region["criteria"].items()
        )
        if region["survey_type"] == "embankment":
            lines.append(
                f"\nSurvey zone {region['id']} (embankment-type, rank {region['rank']} of its "
                f"type): {region['zone_acres']} acres to survey -- a valley compartment anchored "
                f"by a {region['seed_blend_score']}-scoring storage cell, dam reach at the "
                f"downstream end; confidence {region['confidence']}."
            )
            lines.append(
                f"The narrows: {region['pinch_width_ft']} ft crest-to-crest at "
                f"{region['pinch_walk_distance_ft']} ft downstream of the seed (crest-to-crest "
                "width is a survey measure that OVERSTATES dam length). THE HONESTY SPLIT: the "
                f"seed's own criterion signature is {_seed_signature_clause(region)}; the "
                "COMPARTMENT's per-criterion means over the walked ground -- which deliberately "
                "includes low-scoring side slopes and the wall reach, that is its job -- are "
                f"{criteria_clause} (compartment mean {region['mean_suitability']}, max "
                f"{region['max_suitability']})."
            )
            # THE FILL CLAIM, its own sentence. The anchor claim above
            # says whether the storage ground is any good; this says
            # whether water arrives to fill it, and the two are
            # reported apart precisely so the report can state the
            # uncomfortable combination -- good ground, no catchment --
            # rather than average it away into the rank.
            lines.append(
                f"WHAT WOULD FILL IT: {region['pinch_catchment_acres']} acres of catchment drain "
                f"through the dam reach, scoring {region['pinch_drainage_score']} on the drainage "
                f"band (0 below half an acre -- too little to refill a pond -- rising to full "
                f"credit at 2 acres, and 0 again above the "
                f"{region['catchment_ceiling_acres']}-acre ceiling, where a pond would need an "
                f"engineered spillway). This is measured AT THE PINCH CELL, which is the outlet of "
                f"the catchment this compartment would impound -- not at the seed, whose own "
                f"drainage says only whether the storage ground sits in a channel. Rank score "
                f"{region['compartment_rank_score']} combines the two claims at equal weight, a "
                f"provisional rule: read the seed blend and the drainage score, not just the "
                f"composite."
            )
            # The terminal-pinch caveat (accepted, disclosed): a dam
            # reach at the property line / an existing road / the walk
            # limit states plainly that the surveyed extent -- not the
            # terrain -- is what bounded the finding.
            if region.get("pinch_terminal") is not None:
                terminator_noun = {
                    "boundary": "the property line",
                    "road": "the existing road",
                    "walk_bound": "the walk limit",
                }[region["pinch_terminal"]]
                narrowing_clause = (
                    f"the valley continues to narrow beyond {terminator_noun}; "
                    if region.get("still_narrowing_at_termination")
                    else ""
                )
                lines.append(
                    f"TERMINAL PINCH: {narrowing_clause}the marked dam reach is the narrowest "
                    "buildable crossing within the surveyed extent (walked widths ran "
                    f"{region['width_profile_min_ft']}-{region['width_profile_max_ft']} ft "
                    "crest-to-crest)."
                )
            if region.get("truncated_by_boundary") or region.get("truncated_by_road"):
                cut_by = " and ".join(
                    name
                    for name, hit in (
                        ("the property boundary", region.get("truncated_by_boundary")),
                        ("the road exclusion", region.get("truncated_by_road")),
                    )
                    if hit
                )
                lines.append(
                    f"This compartment is TRUNCATED by {cut_by} -- the drawn geometry stops there."
                )
        else:
            lines.append(
                f"\nSurvey zone {region['id']} ({region['survey_type']}-type, rank {region['rank']} of its "
                f"type): {region['zone_acres']} acres to survey, anchored by {region['member_acres']} "
                f"acres of high-suitability ground ({region['member_count']} member region(s)); member-"
                f"cell mean suitability {region['mean_suitability']} (max {region['max_suitability']}), "
                f"confidence {region['confidence']}."
            )
            lines.append(
                f"Per-criterion mean scores over the ANCHORING ground only -- the envelope never "
                f"launders sub-threshold ground into a score (prose may claim only these): "
                f"{criteria_clause}."
            )
        if region.get("either_type_candidate"):
            overlap_clause = "; ".join(
                f"zone {entry['zone_id']} ({entry['overlap_pct']}% of this envelope)"
                for entry in region.get("cross_type_overlaps", [])
            )
            lines.append(
                "EITHER-TYPE CANDIDATE: the two suitability surfaces independently agree about this "
                f"ground -- its envelope substantially overlaps the other pond type's ({overlap_clause}). "
                "This area scores as a candidate for either pond type; evaluate both approaches during "
                "the survey."
            )
        if region.get("sparse_anchor"):
            lines.append(
                "SPARSE ANCHOR: this zone's walkable envelope greatly exceeds the high-suitability "
                "ground anchoring it (under 20% of the envelope is anchor) -- expect scattered good "
                "ground within a larger area, not a solid block."
            )
        gravity = region["gravity"]
        if not gravity["has_service_relationship"]:
            lines.append(
                "No production area within service range -- reported as a flag, not a drop: a water "
                "feature here would serve future or off-plan uses."
            )
        elif gravity["can_gravity_feed"]:
            lines.append(
                f"Sits {gravity['elevation_differential_ft']} ft above production area "
                f"{gravity['production_area_id']} over {gravity['distance_ft']} ft -- a gravity-feed "
                "relationship (ranking context, never a gate)."
            )
        else:
            lines.append(
                f"Sits {abs(gravity['elevation_differential_ft'])} ft BELOW production area "
                f"{gravity['production_area_id']} over {gravity['distance_ft']} ft -- PUMP-REQUIRED: a "
                "real cost/maintenance tradeoff, not a disqualification."
            )
        overlaps = region["overlaps"]

        def _overlap_clause(value, label):
            return f"{label} NOT CHECKED" if value is None else f"{label} {value}%"

        lines.append(
            "Reported overlaps (canopy and production are context for the site visit, never used "
            "to shrink the area; the road figure is the share of this area's pre-clip claim that "
            "the road exclusion REMOVED -- the drawn geometry already stops at mapped farm roads): "
            + ", ".join(
                (
                    _overlap_clause(overlaps["canopy_pct"], "canopy"),
                    _overlap_clause(overlaps["road_pct"], "roads"),
                    _overlap_clause(overlaps["production_pct"], "production ground"),
                )
            )
            + f". Boundary adjacency: {region['boundary_adjacency_pct']}% of this area's perimeter "
            "runs along the property line."
        )
        if region["flags"]:
            lines.append(f"Flags: {', '.join(region['flags'])}.")

    lines.append(
        "\nEvery classification table and weight behind these scores is a provisional v1 prior "
        "(marked TUNE FROM FIRST RUN in water_survey_areas.py); the diagnostic export's suitability "
        "isobands over imagery are the instrument they get tuned from. Ground-truth before "
        "committing to any site."
    )
    return "\n".join(lines)


def _format_keypoints_summary(
    keypoints: Optional[list[dict]], boundary_polygon_utm=None
) -> str:
    """Formats keypoint_detection.detect_keypoints()'s per-valley keypoint
    list (the inflection in each primary valley's long profile -- see that
    module) into a plain, factual data block, the same register as every
    other _format_*_summary() here: measured values only, no narrative.
    WIRED into the report prompt (the Landform section names Keypoint
    Candidates). Imperial at this boundary (feet) -- keypoint dicts stay
    metric; the conversion happens here, once.

    boundary_polygon_utm (the parcel polygon in the DEM's own projected
    CRS -- the same CRS each keypoint's 'point_utm' is in) enables the
    per-keypoint cardinal position via _locative_descriptor(); when it is
    None the position clause is simply omitted, never invented."""
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
            else f"~{round(k['distance_outside_boundary_m'] * _FEET_PER_METER)} ft outside the boundary"
        )
        position_clause = ""
        if boundary_polygon_utm is not None and k.get("point_utm") is not None:
            position_clause = f"in the parcel's {_locative_descriptor(k['point_utm'], boundary_polygon_utm)}, "
        lines.append(
            # Keypoint numbers are 1-based FOR DISPLAY ONLY (k['id'] + 1):
            # ids stay zero-indexed everywhere upstream, same presentation
            # rule as the production patch numbers.
            f"  - Keypoint {k['id'] + 1} (valley {k['valley_id']}): {position_clause}"
            f"{round(k['elevation_m'] * _FEET_PER_METER, 1)} ft elevation, "
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


def _format_irradiance_summary(irradiance: Optional[dict]) -> str:
    """Formats parcel_data.ParcelData.irradiance (irradiance_data.
    get_regional_irradiance_baseline()'s own dict -- its 'status' key says
    whether the numbers are real) for the report prompt: the rooftop solar
    viability read the Permanent Building Site section asks for. WIRED
    now; any non-'ok' status reads as honest no-data, never as a figure."""
    if not irradiance or irradiance.get("status") != "ok":
        return (
            "No regional irradiance baseline available for this run — discuss rooftop "
            "solar viability qualitatively, without quoting a production figure."
        )

    parts = [
        "Regional PVWatts baseline: "
        f"~{round(irradiance['annual_ac_kwh_per_kw'])} AC kWh per kW of installed capacity per year"
    ]
    if irradiance.get("avg_solar_radiation_kwh_per_m2_per_day") is not None:
        parts.append(
            f"average solar radiation {irradiance['avg_solar_radiation_kwh_per_m2_per_day']} kWh/m2/day"
        )
    if irradiance.get("capacity_factor_pct") is not None:
        parts.append(f"capacity factor {irradiance['capacity_factor_pct']}%")
    if irradiance.get("station_distance_miles") is not None:
        parts.append(f"nearest reference weather station {irradiance['station_distance_miles']} miles away")
    return (
        "; ".join(parts) + ". This is parcel-scale regional context — irradiance barely varies "
        "across a property this size, so it informs rooftop solar viability, not site choice."
    )


# One real sentence per stop_reason value a road narrative block can carry --
# road_network_router.route_road_network()'s own four values plus the three
# road_corridors.py adds for outcomes before/after the router runs (see
# _empty_road_network() and the no-anchor early return there). Deliberately a
# closed set: an unrecognized value fails loudly in _format_road_corridor_
# summary() below rather than falling through to a generic, potentially
# misleading sentence.
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
    "no_anchor_given": (
        "No access point was provided for this property, so no road network could "
        "be generated."
    ),
    "no_eligible_anchor": (
        "The provided access point could not be connected to any routable ground "
        "(every nearby cell is excluded or impassable), so no road network was "
        "generated."
    ),
    # RETAINED AS VOCABULARY, THOUGH NOTHING PRODUCES IT ANY MORE. The
    # network-length floor that raised this (road_corridors.MIN_CORRIDOR_
    # LENGTH_METERS) is deleted, so no new run can carry this stop_reason.
    # The sentence stays because this map is a CLOSED SET that RAISES on a
    # value it does not know, and stop_reason is a STORED field: it is
    # written into the narrative block a session keeps, so a design document
    # generated before that floor was removed still carries this value and
    # still has to produce a report. Deleting the entry would turn every one
    # of those into a ValueError at report time -- a hard failure, in the
    # one direction the closed set was never meant to be strict about.
    # Delete it when no stored document can hold it, not before.
    "corridor_too_short": (
        "The only network worth building came out shorter than the minimum "
        "meaningful road length, so none is recommended."
    ),
}


def _format_road_corridor_summary(road_narrative: Optional[dict]) -> str:
    """Formats road_corridors.py's own 'narrative_data' block (see
    build_narrative_data() there for the field contract) for the report
    prompt. This reads ONLY that block -- never the raw network dict it
    replaced here: every length is already in feet, every acreage/grade
    already rounded, at the source module. stop_reason is carried on the
    block for every outcome (including the empty-network ones), and is
    exactly what distinguishes "no production land exists" from
    "production land exists but is unreachable" from "the network already
    reached everything worth reaching" -- very different messages a
    farmer needs told apart. Optional, same reasoning as the other
    DEM/network-backed layers -- a fetch failure (or a caller not yet
    supplying narrative data) shouldn't take down the whole report; step
    4 of the system prompt falls back to its old prose-inference behavior
    when this is empty/unavailable."""
    if not road_narrative or "stop_reason" not in road_narrative:
        return (
            "No road network data available (either the DEM/NHD/SSURGO data "
            "wasn't available for this property, or this run's caller hasn't "
            "supplied the road narrative data) — fall back to topographic "
            "reasoning from Land Shape (step 2) for this section, and say "
            "plainly that it isn't backed by computed network geometry."
        )

    determination = road_narrative["determination"]
    access = road_narrative["access"]

    stop_reason = road_narrative["stop_reason"]
    if stop_reason not in _ROAD_NETWORK_STOP_REASON_SENTENCES:
        raise ValueError(
            f"_format_road_corridor_summary() doesn't recognize road network "
            f"stop_reason {stop_reason!r} -- a new stop_reason value must have been "
            f"added upstream; add its sentence to _ROAD_NETWORK_STOP_REASON_SENTENCES "
            f"rather than let this fall through to a generic, potentially misleading "
            f"message."
        )
    stop_reason_sentence = _ROAD_NETWORK_STOP_REASON_SENTENCES[stop_reason].format(
        unserved_acres=access["unserved_acres"]
    )

    branches = road_narrative["branches"]
    if not branches:
        return stop_reason_sentence

    steep_threshold_pct = determination["steep_grade_threshold_pct"]

    def _steep_section_clause(branch: dict) -> str:
        """A steep-section clause for any branch whose steepest single CELL
        (max_grade_pct) exceeds the block's own steep-grade threshold,
        stating the steep length and peak grade plainly. Gated on
        max_grade_pct, NOT avg_grade_pct -- a route can average a gentle
        grade and still cross a short steep pitch, and that pitch is
        exactly what this surfaces, so a low average must never suppress
        it. Returns '' for a branch with no steep cell (nothing is added
        when no branch is steep)."""
        if branch["max_grade_pct"] <= steep_threshold_pct:
            return ""
        return (
            f" This route includes {branch['steep_ft']}ft above {round(steep_threshold_pct)}% grade, "
            f"reaching {branch['max_grade_pct']}%; that section will need cut-and-fill or a switchback, "
            f"not just routine grading."
        )

    trunk = next((b for b in branches if b["role"] == "trunk"), branches[0])
    lines = [
        f"Recommended road: a single route ({trunk['role']}) {trunk['length_ft']}ft long, "
        f"averaging {trunk['avg_grade_pct']}% grade, newly serving "
        f"{trunk['newly_served_acres']} acre(s) of identified production ground."
        + (" [crosses a production zone]" if trunk["crosses_production_zone"] else "")
        + _steep_section_clause(trunk)
    ]

    branch_by_index = {b["branch_index"]: b for b in branches}
    for branch in branches:
        if branch is trunk:
            continue
        parent = branch_by_index.get(branch["joins_branch_index"])
        parent_note = f"off the {parent['role']}" if parent is not None else "off the network"
        purpose_note = ", reaching the water zone sited in step 3" if branch["role"] == "water_spur" else ""
        crossing_note = " [crosses a production zone]" if branch["crosses_production_zone"] else ""
        lines.append(
            f"  - {branch['length_ft']}ft spur {parent_note}{purpose_note}, "
            f"{branch['avg_grade_pct']}% avg grade, "
            f"{branch['newly_served_acres']} acre(s) newly served{crossing_note}"
            + _steep_section_clause(branch)
        )

    served_pct_clause = (
        f" ({access['served_pct_of_production']}% of the identified production ground)"
        if access["served_pct_of_production"] is not None
        else ""
    )
    lines.append(
        f"\nTotal network length: {access['total_length_ft']}ft, serving "
        f"{access['served_acres']} acre(s) of production ground total{served_pct_clause} -- "
        f"'served' means within {access['service_radius_ft']} ft of the network. "
        f"{stop_reason_sentence}"
    )
    if determination["floodplain_data_is_fallback"]:
        lines.append(
            "\nFloodplain/wet-ground cost scoring used a DEM-only fallback (buffered delineated "
            "valley lines), not real NHD/SSURGO data, because that data wasn't available for this "
            "run."
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


# How the solar narrative block's road_proximity_source values read in
# prose -- which access source the selected site's road distance was
# measured against. Closed set on purpose, same reasoning as
# _ROAD_NETWORK_STOP_REASON_SENTENCES above.
_SOLAR_ROAD_SOURCE_DESCRIPTIONS = {
    "selected_road_corridor": "the property's own selected road corridor",
    "real_mapped_road": "the nearest real mapped road (public road/right-of-way data)",
}


def _format_solar_candidate_zones_summary(solar_narrative: Optional[dict]) -> str:
    """Formats solar_suitability.py's own 'narrative_data' block (see
    build_narrative_data() there for the field contract) for the report
    prompt -- the SELECTED structure site's location and measured
    qualities, read ONLY off that block, never the raw candidate
    geometry/scoring dicts it replaced here. Optional, same reasoning as
    the other DEM/network-backed layers above — a fetch failure shouldn't
    take down the whole report."""
    if not solar_narrative or not solar_narrative.get("site_found"):
        return (
            "No solar structure candidate site identified (either "
            "nothing cleared the exclusion/proximity/suitability "
            "constraint stack, or DEM/road data wasn't available for "
            "this property)."
        )

    site = solar_narrative["selected_site"]
    location = site["location"]
    benefits = site["benefits"]
    gates = solar_narrative["gates"]
    factors = benefits["factors"]

    if location["production_zone_relationship"] == "inside":
        production_note = (
            "production_zone_relationship: inside -- the footprint sits INSIDE a production zone "
            "(intentional — a small structure can coexist with production land)"
        )
    elif location["distance_to_production_edge_ft"] is not None:
        production_note = (
            f"production_zone_relationship: {location['production_zone_relationship']}, "
            f"{location['distance_to_production_edge_ft']}ft from the nearest production zone's edge"
        )
    else:
        production_note = "no production zones identified on this property"

    road_source_description = _SOLAR_ROAD_SOURCE_DESCRIPTIONS.get(gates["road_proximity_source"])
    if location["distance_to_road_ft"] is not None and road_source_description is not None:
        road_note = f"{location['distance_to_road_ft']}ft to {road_source_description}"
    else:
        road_note = "distance to road unknown (no road source was available; the road-proximity constraint was disabled)"

    # distance_to_water_zone_ft stays on the narrative block but is
    # deliberately NOT reported here: distance from a building to a
    # possible future water source isn't actionable and reads as filler.

    if benefits["prime_farmland_conflict"] is True:
        farmland_note = (
            "PRIME FARMLAND CONFLICT: prime (or conditionally prime) farmland soil was found in "
            "this area per SSURGO — solar value vs. agricultural value is a real tradeoff to "
            "present explicitly, not an exclusion."
        )
    elif benefits["prime_farmland_conflict"] is False:
        farmland_note = "No prime farmland classification was found in this area per SSURGO."
    else:
        farmland_note = (
            "Prime-farmland overlap was NOT checked this run (SSURGO unavailable) — do not claim "
            "the site is clear of it."
        )

    tree_zone_note = (
        ""
        if gates["tree_zone_exclusion_checked"]
        else " Tree-zone candidate data was NOT available this run, so the site is NOT confirmed "
        "clear of planned tree-zone ground."
    )

    lines = [
        f"Selected site (rank 1 of {solar_narrative['candidate_count']} ranked candidate(s)): "
        f"score {site['score']}/100, {site['footprint_acres']} acre footprint, in the parcel's "
        f"{location['position_in_parcel']}.",
        f"Location: {production_note}; {road_note}.",
        f"Measured qualities: {benefits['avg_slope_pct']}% average slope, "
        + (
            f"facing {benefits['facing']}"
            if benefits["facing"] is not None
            else "essentially flat (no facing direction)"
        )
        + f"; factor scores (0-100, higher is better): slope {factors['slope']}, "
        f"aspect {factors['aspect']}, shading {factors['shading']}, "
        f"production-edge proximity {factors['production_proximity']}.",
        farmland_note,
        "\nThis is the SELECTED candidate site for a small, fixed-footprint solar-generating "
        "structure (a barn or shed with rooftop panels — not a large ground-mounted array, and not "
        "a permitting-ready placement). It already cleared hard exclusions for existing tree "
        "canopy and the selected water-system zone." + tree_zone_note + " A site sitting inside or "
        "near a production zone is a genuine, intentional option here, not a caveat; treat the "
        "site as a starting point to walk and ground-truth, not a final site plan.",
    ]
    return "\n".join(lines)


def _format_production_areas_summary(production_narrative: Optional[dict]) -> str:
    """Formats production_area_ceiling.py's own 'narrative_data' block (see
    build_narrative_data() there for the field contract) for the report
    prompt -- what ground was selected as production-area candidates and
    what makes each selected patch suitable, read ONLY off that block,
    never the raw scored-patch geometry/scoring dicts. Feeds the LAND
    SHAPE section's production-zone identification (step 2). Optional,
    same reasoning as every other derived layer here."""
    if not production_narrative:
        return (
            "No production-area candidate data available (DEM/soil/canopy data wasn't available "
            "for this property) — identify production land from the elevation grid alone, and say "
            "plainly that it isn't backed by computed candidate geometry."
        )

    parcel = production_narrative["parcel"]
    ceiling = production_narrative["ceiling"]
    gates = production_narrative["gates"]
    patches = production_narrative["patches"]

    lines = [
        f"Parcel: {parcel['total_acres']} acres total; {parcel['slope_passing_acres']} acres clear "
        f"the slope gate; {parcel['eligible_acres']} acres clear every gate; "
        f"{parcel['selected_acres']} acres selected as production candidates "
        f"({parcel['selected_pct_of_parcel']}% of the parcel)."
    ]
    if ceiling["bound"]:
        lines.append(
            f"A {ceiling['cap_pct_of_parcel']}%-of-parcel ceiling trimmed {ceiling['acres_trimmed']} "
            "acres of otherwise-eligible ground (worst-scoring ground first) so room remains for "
            "water systems, trees, roads, and structures."
        )

    def _gate_clause(label: str, acres, available: bool) -> str:
        if not available:
            return f"{label}: not checked this run"
        return f"{label}: {acres} acres excluded"

    lines.append(
        "Exclusions among slope-passing ground — "
        + "; ".join(
            [
                _gate_clause("existing tree canopy", gates["canopy_excluded_acres"], gates["canopy_data_available"]),
                _gate_clause("hydric soil", gates["hydric_excluded_acres"], gates["soil_data_available"]),
                _gate_clause("existing roads", gates["farm_roads_excluded_acres"], gates["road_data_available"]),
                (
                    f"a {gates['boundary_setback_feet']} ft boundary setback: "
                    f"{gates['boundary_setback_excluded_acres']} acres excluded"
                ),
            ]
        )
        + "."
    )

    if not patches:
        lines.append("No candidate patch survived clustering and the minimum-size gate.")
        return "\n".join(lines)

    lines.append(f"{len(patches)} candidate patch(es), best first (scores/factors 0-100, higher is better):")
    for patch in patches:
        aspect_note = (
            f"faces {patch['dominant_aspect']}"
            + (
                f" ({patch['aspect_consistency_pct']}% of its cells within that sector)"
                if patch["aspect_consistency_pct"] is not None
                else ""
            )
            if patch["dominant_aspect"] is not None
            else "no dominant aspect (essentially flat)"
        )
        percentile_note = (
            f", at the {patch['elevation_percentile_of_parcel']} elevation percentile of the parcel "
            "(0 = lowest ground)"
            if patch["elevation_percentile_of_parcel"] is not None
            else ""
        )
        hydric_note = (
            f"; {patch['source_region_hydric_pct']}% of its source region was hydric ground excluded "
            "before it formed"
            if patch["source_region_hydric_pct"] is not None and patch["source_region_hydric_pct"] > 0
            else ""
        )
        hole_note = (
            f"; {patch['hole_count']} interior exclusion hole(s) totalling {patch['hole_acres']} acres"
            if patch["hole_count"]
            else ""
        )
        lines.append(
            # Patch numbers are 1-based FOR DISPLAY ONLY (patch['id'] + 1):
            # ids stay zero-indexed everywhere upstream; "Patch 0" just
            # reads wrong to the person the narrative addresses.
            f"  - Patch {patch['id'] + 1} (rank {patch['rank']}): {patch['area_acres']} acres "
            f"({patch['percent_of_parcel']}% of parcel), in the parcel's "
            f"{patch['position_in_parcel']}, score {patch['score']}/100 "
            f"(slope {patch['factors']['slope_factor']}, size {patch['factors']['size_factor']}, "
            f"aspect {patch['factors']['aspect_factor']}); median slope {patch['slope_median_pct']}% "
            f"(range {patch['slope_min_pct']}-{patch['slope_max_pct']}%); {aspect_note}"
            f"{percentile_note}{hydric_note}{hole_note}."
        )
    lines.append(
        "\nName and refer to these patches by the patch numbers and ranks above — they are the "
        "computed answer to which ground is strong, workable production land, and later steps must "
        "be checked against them."
    )
    return "\n".join(lines)


def _format_tree_zones_summary(tree_narrative: Optional[dict]) -> str:
    """Formats tree_zone_candidates.py's own 'narrative_data' block (see
    build_narrative_data() there for the field contract) for the report
    prompt -- how the tree-suitable ground was determined (search space ->
    selection rules -> qualifying zones), read ONLY off that block. Feeds
    the TREES section (step 5). Optional, same reasoning as every other
    derived layer here."""
    if not tree_narrative:
        return (
            "No tree zone candidate data available (DEM/soil data wasn't available for this "
            "property) — reason about tree placement from climate/water/production context alone, "
            "and say plainly that it isn't backed by computed candidate geometry."
        )

    search_space = tree_narrative["search_space"]
    selection = tree_narrative["selection"]
    gates = tree_narrative["gates"]
    zones = tree_narrative["zones"]

    lines = [
        f"Search space: {search_space['search_space_acres']} of the parcel's "
        f"{search_space['parcel_acres']} acres ({search_space['search_space_pct_of_parcel']}%) "
        f"remained unclaimed by production/water/road candidates and was scored for tree "
        f"suitability; {search_space['claimed_acres']} acres were already claimed (after a "
        f"{search_space['boundary_setback_ft']} ft boundary setback and "
        f"{search_space['production_clearance_ft']} ft / {search_space['water_clearance_ft']} ft "
        "production/water clearances).",
        # The soil marginality and stream proximity factors (and the four
        # factor weights) are deliberately NOT reported here -- internal
        # scoring inputs a farmer has no way to read; the useful facts
        # about why this ground was selected (slope, the soil survey)
        # already reach the prompt. Both fields stay on the narrative
        # block untouched.
        "Ground already under existing tree canopy was excluded before scoring — these candidates "
        "are NEW tree-suitable ground, not ground that's already wooded. Qualifying ground had to "
        f"score at least {selection['min_suitability_score']}/100 on a weighted composite "
        "(weighted most heavily toward hydric-soil overlap, then slope steepness) and cover at "
        f"least {selection['min_zone_acres']} acres — merely being unclaimed leftover land is "
        "deliberately NOT enough to qualify.",
    ]

    unavailable = [
        note
        for available, note in (
            (gates["hydric_data_available"], "hydric-soil"),
            (gates["soil_marginality_data_available"], "farmland-classification"),
            (gates["stream_data_available"], "stream"),
        )
        if not available
    ]
    if unavailable:
        lines.append(
            f"NOTE: {', '.join(unavailable)} data was unavailable this run, so those factor(s) "
            "defaulted to a neutral value rather than a real measurement."
        )

    if not zones:
        lines.append("No leftover ground cleared the suitability threshold — no tree zone candidates.")
    else:
        lines.append(
            f"{len(zones)} tree zone candidate(s), best first (score and factors 0-100, higher is "
            "better):"
        )
        for zone in zones:
            factors = zone["factors"]
            lines.append(
                f"  - Rank {zone['rank']}: {zone['area_acres']} acres, in the parcel's "
                f"{zone['position_in_parcel']}, score {zone['score']}/100 "
                f"(hydric overlap {factors['hydric_overlap']}, slope {factors['slope']}), "
                f"average slope {zone['avg_slope_pct']}%."
            )

    lines.append(
        "\nThese identify GENERAL tree-suitable ground only — assigning each zone a function "
        "(windbreak, habitat corridor) is the narrative's job, reasoning from "
        "wind direction (step 1) and water features (step 3); this is not a species "
        "recommendation, and every zone should be ground-truthed before planting."
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
    narrative_data: Optional[dict] = None,
    boundary_polygon_utm=None,
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
    now WIRED into the LLM prompt as the KEYPOINT CANDIDATES data block
    (the Landform section reads it) -- the staged _format_keypoints_
    summary() seam earlier branches carried is closed. boundary_polygon_
    utm (the parcel polygon in the DEM's own projected CRS -- e.g.
    pipeline_context.PipelineContext.boundary_polygon_utm) enables each
    keypoint's cardinal position on the map (_locative_descriptor());
    when None the positions are simply omitted, never invented.

    irradiance (parcel_data.ParcelData.irradiance -- get_regional_
    irradiance_baseline()'s own dict, 'status' key says whether the
    numbers are real) is now WIRED into the LLM prompt as the SOLAR
    IRRADIANCE data block -- the rooftop solar viability read the
    Permanent Building Site section asks for. A non-'ok' status reads as
    honest no-data.

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

    narrative_data is pipeline_context.PipelineContext.narrative_data --
    the per-module narrative blocks each KSOP module attaches to its own
    identify_*() result under the narrative_data convention, keyed by
    producing module ("production_area_ceiling", "water_candidate_zones",
    "road_corridors", "solar_suitability", "tree_zone_candidates"). THIS
    is what the water/road/solar/production/tree formatting functions
    below read now, instead of raw geometry/scoring dicts: every value in
    a block is pre-digested at its source module (FINAL, imperial,
    rounded), so the formatters write data lines from it with no unit
    conversion or computation of their own. When None (or a module's key
    is absent/None), each formatter falls back to its own honest
    "no data available" text -- so a caller not yet supplying it degrades
    exactly like a data outage, never to stale or invented content. The
    older water_candidate_zones_geojson/solar_candidate_zones_geojson/
    road_network parameters are still accepted and STORED for
    compatibility, but no longer feed any formatter -- narrative_data
    replaced them at that boundary.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable not set. Get a key from "
            "console.anthropic.com and run: export ANTHROPIC_API_KEY='sk-ant-...'"
        )

    client = Anthropic(api_key=api_key)

    # Every KSOP-derived block below is formatted from narrative_data (the
    # per-module blocks captured on PipelineContext) -- see this function's
    # own docstring. An absent module key formats as that block's honest
    # "no data available" text.
    narrative_data = narrative_data or {}

    # Block headers use the map legend's own feature-class names (Keypoint
    # Candidates, Production Areas, Water System Survey Area, Suggested
    # Road Corridor, Tree Crop Areas, Permanent Building Site) so the
    # narrative's references and the data blocks resolve to the same names
    # a reader sees on the map.
    data_summary = f"""CLIMATE DATA:
{_format_climate_summary(climate_summary)}

SOIL DATA (SSURGO soil survey):
{_format_soil_summary(soil_components)}

ELEVATION (USGS):
{_format_elevation_summary(elevation_grid)}

PRODUCTION AREAS (computed candidates, ceiling-trimmed):
{_format_production_areas_summary(narrative_data.get("production_area_ceiling"))}

KEYPOINT CANDIDATES (DEM-derived):
{_format_keypoints_summary(keypoints, boundary_polygon_utm)}

WATER FEATURES (mapped NHD streams/water bodies):
{_format_water_summary(water_features)}

SATELLITE IMAGERY / LAND COVER (NDVI-derived):
{_format_imagery_summary(imagery_summary)}

WATER SYSTEM SURVEY AREAS (computed):
{_format_water_survey_areas_summary(narrative_data.get("water_survey_areas"))}

SUGGESTED ROAD CORRIDOR (computed network, grown from the real access point):
{_format_road_corridor_summary(narrative_data.get("road_corridors"))}

TREE CROP AREAS (computed candidates):
{_format_tree_zones_summary(narrative_data.get("tree_zone_candidates"))}

PERMANENT BUILDING SITE (computed, selected structure site):
{_format_solar_candidate_zones_summary(narrative_data.get("solar_suitability"))}

SOLAR IRRADIANCE (regional baseline):
{_format_irradiance_summary(irradiance)}"""

    message = client.messages.create(
        model=MODEL,
        max_tokens=20000,
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
