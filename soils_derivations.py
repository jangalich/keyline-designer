"""
soils_derivations.py

THE SOILS & GEOLOGY SECTION'S DERIVATIONS (section VII, branch 12 phase
1), computed on the report path from the session's Layer 1 soil reads and
the report layer's survey and geology blocks. Everything here is in the
survey's own centimetres and the DEM's cell counts; soils_section (phase
2) converts and formats. No colour, no page text, no network, and NO
ADVICE: the section is an INVENTORY of what the survey recorded, never a
statement about what to do with it.

    soils_inputs_from_context(context, document, report_data) -> SoilsInputs
    derive(inputs)                                            -> SoilsDerived

THE REPORT'S FOURTH PASS AT SSURGO REPEATS NOTHING. The seasonal water
table is in Water, the road-construction ratings are in Access, the
woodland productivity is in Trees; this section carries the core survey
and points at the rest in one sentence. Of its own nine map unit table
columns, FOUR ARE ALREADY IN HAND off Layer 1 and are read, not fetched:
drainage class and hydrologic group off ParcelData.soil_components,
saturated hydraulic conductivity off ParcelData.saturated_hydraulic_
conductivity, farmland classification off ParcelData.farmland_
classification. The K factor comes off ParcelData.erosion_factor the same
way. Only the symbol, the surface horizon's properties, the capability
class, the T factor and the depth to bedrock are fetched, and those are
one query (soil_survey).

THE GROUND IS PARTITIONED THE WAY EVERY OTHER SECTION PARTITIONS IT --
water_derivations.derive_hydric's map-unit cell grid, over the same
Layer 1 polygons, so the same cell falls in the same unit in Water,
Access, Trees and here, and every acreage column adds to the cover's
parcel acreage through landform_section.allocate_exactly. Deriving a
second partition from the same polygons would be a second answer to a
question already answered.

ONE COMPONENT PER MAP UNIT CARRIES THE ROW. A map unit is a named
mixture: "Guernsey-Vandergrift silt loams" is 55% Guernsey and 35%
Vandergrift, each with its own texture, its own pH and its own bedrock.
The physical-properties row reports the DOMINANT MAJOR COMPONENT
throughout (soil_survey.dominant_major) rather than weighting the figures
by percentage, because the named texture class cannot be averaged and a
row whose numbers were means and whose texture was one component's would
describe a soil that does not exist. The caption names the rule.

CLASSIFICATION IS BY ACRES, NOT BY UNIT. Capability class and farmland
classification are map-unit properties, so a unit's whole ground goes to
its class and the classes partition the parcel: "class 4e, 5.3 acres" is
the statement, not "three of seven units". Capability is the dominant
major component's (SSURGO assigns it per component); farmland
classification is the map unit's own farmlndcl.

BEDROCK IS A DEPTH OR A BOUND, NEVER A BLANK. A component with no bedrock
restriction was described to some depth and bedrock was not reached
there, so the figure is "deeper than" that depth, which is the cell kind
the Water section's water table already uses. A restriction that is NOT
bedrock is carried separately and states itself: the reference parcel's
Ernest component, the dominant soil of a 3.1-acre unit, has a FRAGIPAN at
71 cm and no bedrock at all, while muaggatt's own brockdepmin reports
152 cm for that unit off a companion component. A reader given 152 cm
alone would conclude there is five feet of rooting.

THE SURVEY IS NOT A SOIL TEST, and organic matter and pH are where that
bites hardest -- they are the two figures a grower would otherwise act on
and the two that move most between one field and the next within a single
map unit. The values here are REPRESENTATIVE OF A MAP UNIT, not measured
on this ground. soils_section states it in one sentence at the point of
use; this module records `representative_only` on the block so the
statement cannot be dropped by accident.

GEOLOGY IS A VALUE, NOT A MAP. bedrock_geology's block, carried through
unchanged but for the ordering its own parser applied. See that module
for why one line is the honest product at 1:1,000,000.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import soil_survey as ss
import water_derivations as wd
from raster_grid import SQUARE_METERS_PER_ACRE

CENTIMETERS_PER_INCH = 2.54

# The land capability classes SSURGO assigns, in the survey's own order.
# 1 is best; 8 has no commercial plant production. The section tables
# only those the parcel carries.
CAPABILITY_CLASSES = ("1", "2", "3", "4", "5", "6", "7", "8")

# A map unit with no farmland classification served, and one with no
# capability class on its dominant component: distinct from "not prime
# farmland", which IS a classification.
NOT_CLASSIFIED = "not classified"
NO_DATA = "no data"

# SSURGO's farmland classification values group into three for a table;
# the survey's own full string is carried and printed, these only order
# the rows so prime land reads first.
_FARMLAND_ORDER = ("All areas are prime farmland", "Prime farmland if", "Farmland of statewide importance",
                   "Farmland of local importance", "Farmland of unique importance", "Not prime farmland")


@dataclass
class _GridInputs:
    """THE FOUR FIELDS water_derivations.grid_bookkeeping() AND
    derive_hydric() READ, and nothing else.

    Those two build the report's map-unit cell partition and this section
    reuses them rather than deriving a second answer to the same question
    (see the module docstring). Handing them a WaterInputs would mean
    inventing a slope grid, a valley list and five report blocks this
    section does not have and they do not read -- a fabricated input is
    worse than a narrow one. test_soils_derivations.py asserts the
    partition this produces is Water's own, cell for cell, on the
    reference parcel, which is what actually holds the reuse honest."""

    dem: dict
    boundary_polygon_utm: object
    soil_components: list
    soil_geometries: dict


@dataclass
class SoilsInputs:
    dem: dict
    boundary_polygon_utm: object
    # ParcelData's Layer 1 soil reads -- every one of these is already in
    # the session and none of them is fetched again here.
    soil_components: list            # mukey, muname, compname, comppct_r, drainagecl, hydgrp, ...
    soil_geometries: dict            # {mukey: GeoJSON geometry (WGS84)}
    farmland_classification: list    # [{'mukey', 'muname', 'farmland_classification'}]
    erosion_factor: list             # [{'mukey', 'compname', 'comppct_r', 'kwfact'}]
    saturated_hydraulic_conductivity: list   # [{'mukey', 'compname', 'comppct_r', 'ksat_r'}]
    parcel_acres: float
    retrieved_on: date
    # The report layer's blocks, each None when it degraded.
    soil_survey: Optional[dict]
    bedrock_geology: Optional[dict]
    unavailable: dict = field(default_factory=dict)


@dataclass
class SoilsDerived:
    cells: dict
    map_units: dict       # {mukey: the row's every value, unformatted}
    order: list           # mukeys, largest share of the parcel first
    properties: dict      # {mukey: the surface horizon and the depths}
    capability: dict      # {'counts': {label: cells}, 'labels': [...]}
    farmland: dict        # {'counts': {value: cells}, 'values': [...]}
    erosion: dict         # {mukey: {'kwfact', 'tfact'}}
    geology: Optional[dict]
    survey_areas: list
    representative_only: bool


# ======================================================================
# Inputs
# ======================================================================


def _created_on(document: dict) -> date:
    stamp = (document or {}).get("created_at")
    if not stamp:
        return date.today()
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).date()


def soils_inputs_from_context(context, document: dict, report_data) -> SoilsInputs:
    """Every read off the session and the report data. Raises when the
    exclusion result carries no slope grid, as the Landform, Water,
    Access and Trees reads do -- the grid bookkeeping this section shares
    with Water is built on the same session products."""
    exclusion = context.exclusion_zones or {}
    if exclusion.get("slope_pct") is None:
        raise ValueError("the session's exclusion result carries no slope grid")
    parcel = context.parcel_data
    boundary = context.boundary_polygon_utm
    return SoilsInputs(
        dem=context.dem,
        boundary_polygon_utm=boundary,
        soil_components=list(getattr(parcel, "soil_components", None) or []),
        soil_geometries=dict(getattr(parcel, "soil_geometries", None) or {}),
        farmland_classification=list(getattr(parcel, "farmland_classification", None) or []),
        erosion_factor=list(getattr(parcel, "erosion_factor", None) or []),
        saturated_hydraulic_conductivity=list(getattr(parcel, "saturated_hydraulic_conductivity", None) or []),
        parcel_acres=boundary.area / SQUARE_METERS_PER_ACRE,
        retrieved_on=_created_on(document),
        soil_survey=getattr(report_data, "soil_survey", None),
        bedrock_geology=getattr(report_data, "bedrock_geology", None),
        unavailable=dict(getattr(report_data, "unavailable", None) or {}),
    )


# ======================================================================
# Layer 1's rows, keyed by map unit
# ======================================================================


def dominant_rows_by_mukey(rows: list) -> dict:
    """
    {mukey: the row of the largest component} for any Layer 1 soil query.

    Every one of them orders by comppct_r DESC and repeats muname on each
    row, so the FIRST row for a map unit is its dominant component --
    this codebase's standing convention (soil_data.get_soil_data_for_
    polygon's docstring). Ties keep the order the service gave.
    """
    dominant = {}
    for row in rows or []:
        mukey = str(row.get("mukey"))
        if mukey in ("None", ""):
            continue
        share = row.get("comppct_r")
        try:
            share = float(share)
        except (TypeError, ValueError):
            share = 0.0
        if mukey not in dominant or share > dominant[mukey][0]:
            dominant[mukey] = (share, row)
    return {mukey: row for mukey, (_, row) in dominant.items()}


def _number(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _string(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def derive_map_units(inputs: SoilsInputs, hydric: dict) -> tuple:
    """
    ({mukey: the map unit table's row, unformatted}, [mukey, ...] largest
    first).

    The row: symbol, name, cells, drainage class, hydrologic group, Ksat,
    capability class and subclass, farmland classification -- four of
    which come off Layer 1 (`components`, `ksat`, `farmland`) and are not
    fetched again, and two of which (the symbol and the capability) come
    off the report layer's survey block and are None when it degraded.
    """
    survey = inputs.soil_survey
    components = dominant_rows_by_mukey(inputs.soil_components)
    ksat = dominant_rows_by_mukey(inputs.saturated_hydraulic_conductivity)
    farmland = {str(r.get("mukey")): _string(r.get("farmland_classification")) for r in inputs.farmland_classification}

    map_units = {}
    for mukey, unit in hydric["map_units"].items():
        component = components.get(mukey, {})
        dominant = ss.dominant_major(survey, mukey) if survey else None
        survey_unit = (survey or {}).get("map_units", {}).get(mukey, {})
        map_units[mukey] = {
            "mukey": mukey,
            "musym": survey_unit.get("musym"),
            "muname": unit["muname"],
            "cells": unit["cells"],
            "geometry_utm": unit["geometry_utm"],
            "drainage_class": _string(component.get("drainagecl")),
            "hydrologic_group": _string(component.get("hydgrp")),
            "ksat_um_s": _number((ksat.get(mukey) or {}).get("ksat_r")),
            "capability_class": (dominant or {}).get("capability_class"),
            "capability_subclass": (dominant or {}).get("capability_subclass"),
            "farmland": farmland.get(mukey),
            "dominant_compname": (dominant or {}).get("compname"),
            "dominant_comppct": (dominant or {}).get("comppct"),
        }
    order = sorted(map_units, key=lambda m: (-map_units[m]["cells"], map_units[m]["musym"] or "", m))
    return map_units, order


# ======================================================================
# Physical properties
# ======================================================================


def derive_properties(inputs: SoilsInputs, map_units: dict) -> dict:
    """
    {mukey: {'compname', 'comppct', 'hzname', 'top_cm', 'bottom_cm',
             'texture', 'texture_class', 'sand_pct', 'silt_pct',
             'clay_pct', 'awc', 'om_pct', 'ph',
             'bedrock_cm', 'bedrock_kind', 'bedrock_is_bound',
             'restriction_cm', 'restriction_kind', 'aws0150_cm'}}

    The DOMINANT MAJOR COMPONENT's surface horizon, one rule for the whole
    row (see the module docstring). Empty when the survey layer degraded.

    `bedrock_is_bound` is True when no bedrock restriction was described
    and `bedrock_cm` is therefore the depth the survey looked to rather
    than a depth to rock -- the caller prints it as a bound. The sand,
    silt and clay percentages are carried as the survey gives them and are
    NOT renormalised: they are independent representative values and may
    not total exactly 100.
    """
    survey = inputs.soil_survey
    if not survey:
        return {}
    properties = {}
    for mukey in map_units:
        component = ss.dominant_major(survey, mukey)
        if component is None:
            continue
        horizon = component["horizon"] or {}
        bedrock, is_bound = component["bedrock_cm"], False
        if bedrock is None:
            bedrock, is_bound = component["described_bottom_cm"], True
        properties[mukey] = {
            "compname": component["compname"],
            "comppct": component["comppct"],
            "hzname": horizon.get("hzname"),
            "top_cm": horizon.get("top_cm"),
            "bottom_cm": horizon.get("bottom_cm"),
            "texture": horizon.get("texture"),
            "texture_class": horizon.get("texture_class"),
            "sand_pct": horizon.get("sand_pct"),
            "silt_pct": horizon.get("silt_pct"),
            "clay_pct": horizon.get("clay_pct"),
            "awc": horizon.get("awc"),
            "om_pct": horizon.get("om_pct"),
            "ph": horizon.get("ph"),
            "bedrock_cm": bedrock,
            "bedrock_kind": component["bedrock_kind"],
            "bedrock_is_bound": is_bound,
            "restriction_cm": component["restriction_cm"],
            "restriction_kind": component["restriction_kind"],
            "aws0150_cm": (survey["map_units"].get(mukey) or {}).get("aws0150_cm"),
        }
    return properties


def texture_headline(properties: dict, map_units: dict) -> Optional[dict]:
    """
    {'texture', 'cells', 'units': n} for the texture class covering the
    most ground, or None with no survey. Texture leads the page, and
    "silt loam" is the phrase a grower uses, so the headline is the CLASS
    (chtexture.texcl -- "Silt loam"), not the survey's modified phrase
    ("Channery silt loam"), which describes the rock fragments in it and
    would split one texture into two headlines.
    """
    totals, units = {}, {}
    for mukey, block in properties.items():
        name = block["texture_class"]
        if not name:
            continue
        totals[name] = totals.get(name, 0) + map_units[mukey]["cells"]
        units[name] = units.get(name, 0) + 1
    if not totals:
        return None
    name = max(totals, key=lambda n: (totals[n], units[n]))
    return {"texture": name, "cells": totals[name], "units": units[name]}


def restriction_notes(properties: dict, map_units: dict) -> list:
    """
    [{'musym', 'compname', 'kind', 'depth_cm', 'bedrock_cm',
      'bedrock_is_bound'}] for every unit whose dominant component has a
    restriction that is NOT bedrock, shallower than whatever the bedrock
    column says.

    The Ernest fragipan is why this exists: a root restriction that is not
    rock, on a unit whose bedrock column reads as open ground. Ordered by
    the ground each unit covers, largest first.
    """
    notes = []
    for mukey, block in properties.items():
        depth, kind = block["restriction_cm"], block["restriction_kind"]
        if depth is None or not kind:
            continue
        if block["bedrock_cm"] is not None and depth >= block["bedrock_cm"]:
            continue
        notes.append({
            "mukey": mukey,
            "musym": map_units[mukey]["musym"],
            "compname": block["compname"],
            "kind": kind,
            "depth_cm": depth,
            "bedrock_cm": block["bedrock_cm"],
            "bedrock_is_bound": block["bedrock_is_bound"],
            "cells": map_units[mukey]["cells"],
        })
    notes.sort(key=lambda n: -n["cells"])
    return notes


# ======================================================================
# Classification
# ======================================================================


def capability_label(component_class: Optional[str], subclass: Optional[str]) -> str:
    """"4e", "3w", or "4" where the survey assigned no subclass. NO_DATA
    where it assigned no class -- distinct from class 8, which is a
    classification meaning the land supports no commercial production."""
    if not component_class:
        return NO_DATA
    return f"{component_class}{subclass or ''}"


def derive_capability(map_units: dict) -> dict:
    """{'counts': {label: cells}, 'labels': [label, ...]} -- the parcel's
    ground partitioned by land capability class and subclass, ordered by
    class then subclass, NO_DATA last. A partition: every on-parcel cell
    that fell in a map unit falls in exactly one label."""
    counts = {}
    for unit in map_units.values():
        label = capability_label(unit["capability_class"], unit["capability_subclass"])
        counts[label] = counts.get(label, 0) + unit["cells"]

    def key(label):
        # The CLASS is the leading digits and the SUBCLASS is what
        # follows, so the split is on the digit run, not on one character:
        # taking label[0] would file a two-digit class under its first
        # digit and sort it ahead of everything.
        if label == NO_DATA:
            return (len(CAPABILITY_CLASSES) + 1, 0, "")
        digits = ""
        for character in label:
            if not character.isdigit():
                break
            digits += character
        rank = CAPABILITY_CLASSES.index(digits) if digits in CAPABILITY_CLASSES else len(CAPABILITY_CLASSES)
        return (rank, int(digits) if digits else 0, label[len(digits):])

    return {"counts": counts, "labels": sorted(counts, key=key)}


def derive_farmland(map_units: dict) -> dict:
    """{'counts': {value: cells}, 'values': [value, ...]} -- the parcel's
    ground by SSURGO's own farmland classification string, prime land
    first. A unit the survey classified as nothing is NOT_CLASSIFIED,
    which is not the same as "Not prime farmland"."""
    counts = {}
    for unit in map_units.values():
        value = unit["farmland"] or NOT_CLASSIFIED
        counts[value] = counts.get(value, 0) + unit["cells"]

    def key(value):
        for index, prefix in enumerate(_FARMLAND_ORDER):
            if value.startswith(prefix):
                return (index, value)
        return (len(_FARMLAND_ORDER), value)

    return {"counts": counts, "values": sorted(counts, key=key)}


def derive_erosion(inputs: SoilsInputs, map_units: dict) -> dict:
    """
    {mukey: {'kwfact', 'tfact'}} -- the K factor off LAYER 1's erosion
    query (already fetched; it is the same surface horizon soil_survey
    reads, by the same SQL rule) and the T factor off the report layer's
    survey block.

    K is the whole soil's erodibility, dimensionless; T is the soil loss
    the survey tolerates in tons per acre per year. Both are the dominant
    component's, the rule the rest of the row follows.
    """
    kwfact = dominant_rows_by_mukey(inputs.erosion_factor)
    survey = inputs.soil_survey
    erosion = {}
    for mukey in map_units:
        dominant = ss.dominant_major(survey, mukey) if survey else None
        erosion[mukey] = {
            "kwfact": _number((kwfact.get(mukey) or {}).get("kwfact")),
            "tfact": (dominant or {}).get("tfact"),
        }
    return erosion


# ======================================================================
# The block
# ======================================================================


def acre_values(derived: SoilsDerived, counts: list) -> tuple:
    """(acres, percents) for a list of cell counts, both allocated
    EXACTLY -- the shared allocator, so an acreage column sums to the
    cover's parcel acreage and a percent column to 100.0."""
    from landform_section import allocate_exactly

    parcel_acres = derived.cells["on_parcel_count"] * derived.cells["cell_acres"]
    return (allocate_exactly(counts, parcel_acres, 1), allocate_exactly(counts, 100.0, 1))


def derive(inputs: SoilsInputs) -> SoilsDerived:
    """Every figure the section prints, in the survey's own units. The
    map-unit cell grid is Water's (water_derivations), over Layer 1's
    polygons, so the partition is the report's one partition."""
    grid_inputs = _GridInputs(
        dem=inputs.dem,
        boundary_polygon_utm=inputs.boundary_polygon_utm,
        soil_components=inputs.soil_components,
        soil_geometries=inputs.soil_geometries,
    )
    # COMPUTED AGAIN HERE, KNOWINGLY -- NOT A BUG, AND NOT FREE TO REMOVE.
    # The map-unit cell grid (grid_bookkeeping + derive_hydric) runs once in
    # each section that reads it: Water, Access, Trees and Soils, four times
    # per report, over the same Layer 1 rows, to the same answer (test_soils_
    # derivations.py holds the partitions equal). Measured at about 0.05-0.1 s
    # a time and no network (diagnose_report_generation_time.py), so the repeat
    # costs under half a second and cannot fail on a flaky source. Sharing one
    # result means threading it through every section's inputs; worth doing
    # only if the report's compute ever matters beside its fetches.
    cells = wd.grid_bookkeeping(grid_inputs)
    hydric = wd.derive_hydric(grid_inputs, cells)

    map_units, order = derive_map_units(inputs, hydric)
    properties = derive_properties(inputs, map_units)
    return SoilsDerived(
        cells=cells,
        map_units=map_units,
        order=order,
        properties=properties,
        capability=derive_capability(map_units),
        farmland=derive_farmland(map_units),
        erosion=derive_erosion(inputs, map_units),
        geology=inputs.bedrock_geology,
        survey_areas=list((inputs.soil_survey or {}).get("survey_areas") or []),
        representative_only=True,
    )
