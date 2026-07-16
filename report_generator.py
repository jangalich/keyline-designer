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
Permanence orders design decisions from least changeable to most changeable: climate,
landform/geology, water, access/roads, trees/windbreaks, buildings, fencing/subdivision,
soil fertility, and finally aesthetics.

You will be given real climate and geospatial data for a specific property: historical
climate data (prevailing wind, rainfall, temperature), soil survey data, an elevation
grid, nearby surface water features, and a satellite-derived land cover snapshot
(NDVI-based: percent bare/degraded ground, low vegetation, high-vigor vegetation, and
open water). Your job is to:

1. Summarize what the data reveals about this property's climate, landform, water, and
   soil characteristics — in plain, direct language a landowner (not a GIS professional)
   can understand.
2. Reason about how these factors interact — for example, how prevailing wind direction
   should inform windbreak orientation, how rainfall intensity should inform pond/swale
   sizing, or how slope and soil drainage together suggest where water tends to move
   and pool.
3. Suggest candidate design considerations following Scale of Permanence order: what
   the climate/landform/water pattern suggests about windbreak placement, keyline
   placement, pond/dam siting, access road routing, and where NOT to place permanent
   structures.
4. Be honest about the limits of this data. This is a first-pass analysis from public
   data, not a substitute for walking the land or a professional site visit. Do not
   invent specifics the data doesn't support.

Note on climate data specifically: prevailing wind and rainfall intensity are genuinely
design-relevant and should shape your recommendations directly. Temperature data is
useful context (mention it briefly) but is more relevant to future crop/species
selection than to the land design decisions covered here — don't over-invest in
reasoning about it.

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

Write in clear, direct prose. Use section headers. Avoid hedging on every sentence,
but do flag genuine uncertainty where the data is thin or ambiguous."""


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
) -> str:
    """
    Given the outputs of the data-fetching modules, generates a narrative
    Scale of Permanence report via the Claude API. climate_summary (from
    climate_data.py) and imagery_summary (from imagery_data.py) are optional
    so existing callers built before those layers existed don't break — but
    including them produces a meaningfully better report, since climate is
    literally the first item in the Scale of Permanence framework and
    imagery gives Claude a current land-cover cross-check against the soil
    data.
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
{_format_imagery_summary(imagery_summary)}"""

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
