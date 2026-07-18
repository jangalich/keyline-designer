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

4. FARM ROADS (contour or ridge placement). When SUGGESTED ROAD CORRIDOR data is
   provided below (DEM-derived contour-band and/or ridge-top candidates, already
   screened against production zones, pond/water-system zones, floodplain/hydric
   ground, and erosion-prone soil, and ranked by grade consistency, exclusion-zone
   avoidance, and length), narrate FROM those ranked candidates — name/rank them,
   compare grade and length where more than one is offered, and say plainly if a
   candidate's boundary connection point is flagged as arbitrary (no real access-point
   data) rather than treating it as a confirmed entry. Do not invent a corridor of your
   own where real candidates are provided. If no candidate data is available for this
   property (or none cleared the constraint stack), fall back to describing routing
   that would follow the ridge or contour lines from step 2 and avoid the water
   infrastructure/catchments from step 3, and say plainly that this is an unverified
   topographic suggestion, not a placement backed by computed candidate geometry.
   Either way, explicitly check the routing against the production zones from step 2 —
   a road should not run through one without saying so — and note that no surveyed
   parcel or easement data feeds this step regardless of which path was used.

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
   SOLAR INFRASTRUCTURE CANDIDATE ZONE data is provided below (DEM-derived, ranked
   candidates already screened for slope, aspect, shading, distance from production
   land, and proximity to a mapped road), use it as the concrete basis for any solar
   siting discussion: compare the ranked candidates against each other by name/rank
   rather than inventing an unranked one, and if a candidate is flagged with a prime-
   farmland conflict, present that explicitly as a tradeoff (solar value vs. agricultural
   value of that land) rather than silently picking a side. Do not present any single
   candidate as a forced final answer when multiple are close in score — say so, and let
   the ranked list stand as real options.

7. SUBDIVISION FENCES. Propose paddock/subdivision lines that follow the zones and
   infrastructure already established in steps 2-6 (land-shape zones, water points,
   roads, tree lines, building sites) rather than an arbitrary grid, so fencing
   reinforces those decisions instead of cutting across them. Note that no legal parcel
   or ownership-boundary data feeds this step.

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

DATA HONESTY: Farm Roads has real candidate-corridor geometry (step 4) when the
constraint stack produced any; Permanent Buildings has real candidate-zone geometry for
solar siting specifically (step 6), but nothing else about building placement, and
Subdivision Fences has no dedicated data source at all — that one is inferred only from
the climate, elevation, water, and imagery data plus whatever zones/corridors earlier
steps established. When reasoning about parts of these sections that AREN'T backed by
real candidate geometry (fencing in general, non-solar building siting, or Farm Roads
when no corridor candidates were available), say plainly that no dedicated
infrastructure/parcel/zoning data exists there, rather than inventing a specific-
sounding recommendation the data can't support (an exact building footprint, a precise
fence-post count, a named legal easement). This is a first-pass analysis from public
data, not a substitute for walking the land or a professional site visit.

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


def _format_road_corridor_summary(zones_geojson: Optional[dict]) -> str:
    """Formats road_corridors.py's "suggested_road_corridor" layer (see
    that module for the contour-band/ridge-top generation and constraint
    stack behind it) for the report prompt. Optional, same reasoning as
    the other DEM/network-backed layers — a fetch failure shouldn't take
    down the whole report; step 4 of the system prompt falls back to its
    old prose-inference behavior when this is empty/unavailable."""
    if not zones_geojson or not zones_geojson.get("features"):
        return (
            "No suggested road corridor candidates identified (either "
            "nothing cleared the constraint stack, or DEM/NHD/SSURGO data "
            "wasn't available for this property) — fall back to "
            "topographic reasoning from Land Shape (step 2) for this "
            "section, and say plainly that it isn't backed by computed "
            "candidate geometry."
        )

    lines = [f"{len(zones_geojson['features'])} ranked candidate corridor(s) identified:"]
    for feature in zones_geojson["features"]:
        props = feature["properties"]
        anchor_note = (
            "boundary connection point is ARBITRARY (no real access-point data)"
            if props.get("connection_point_is_arbitrary")
            else "anchored near a real mapped road"
        )
        lines.append(
            f"  - Rank {props['rank']} ({props['corridor_type']}, score {props['suitability_score']}/100): "
            f"{props['avg_grade_pct']}% avg grade, {props['length_ft']}ft long, {anchor_note}"
        )
    lines.append(
        "\nThese are ranked CANDIDATE CORRIDORS (contour-band and/or ridge-top, no default "
        "preference between the two types), not a single forced routing — narrate from this "
        "geometry rather than inventing a corridor, compare candidates where more than one is "
        "offered, and say plainly wherever a connection point is flagged arbitrary."
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
        lines.append(
            f"  - Rank {props['rank']} (score {props['suitability_score']}/100): "
            f"{props['avg_slope_pct']}% slope, {props['aspect']}-facing, "
            f"{distance_to_road}, {props['distance_to_production_zone_ft']}ft from "
            f"nearest production zone{conflict}"
        )
    lines.append(
        "\nThese are ranked CANDIDATE ZONES, not a single forced placement — compare "
        "them against each other in the narrative rather than picking one unprompted, "
        "and note prime-farmland conflicts as a real tradeoff where flagged."
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
    road_corridor_candidates_geojson: Optional[dict] = None,
) -> str:
    """
    Given the outputs of the data-fetching modules, generates a narrative
    Scale of Permanence report via the Claude API. climate_summary (from
    climate_data.py), imagery_summary (from imagery_data.py),
    water_candidate_zones_geojson (the "water_system_candidate" layer from
    water_candidate_zones.py), solar_candidate_zones_geojson (the
    "solar_infrastructure" layer from solar_suitability.py), and
    road_corridor_candidates_geojson (the "suggested_road_corridor" layer
    from road_corridors.py) are all optional so existing callers built
    before those layers existed don't break — but including them produces
    a meaningfully better report: climate is literally the first item in
    the Scale of Permanence framework, imagery gives Claude a current
    land-cover cross-check against the soil data, the water candidate
    zones give the WATER SUPPLY section a DEM-grounded answer to "where"
    instead of reasoning from the coarse elevation grid alone, the road
    corridor candidates do the same for FARM ROADS, and the solar
    candidate zones do the same for PERMANENT BUILDINGS' solar siting
    discussion.
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

SUGGESTED ROAD CORRIDOR CANDIDATES (contour-band/ridge-top, DEM-derived):
{_format_road_corridor_summary(road_corridor_candidates_geojson)}

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
