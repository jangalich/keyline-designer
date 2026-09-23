"""
report_outline.py

THE SITE DATA REPORT'S FIXED SECTION ORDER -- the Scale of Permanence,
as site-data-report-proposal.md lays it out -- and the numeral each
section carries because of its place in it.

    SECTION_OUTLINE          the seven section names, in order
    section_number("Climate") -> "II"

A SECTION'S NUMBER IS ITS PLACE IN THE OUTLINE, NOT ITS PLACE IN THE
RENDERED REPORT. Climate is II whether or not Site overview renders ahead
of it; a report that carries only the Climate section still says
"II · Climate", because the number tells the reader where the section
sits in the method, and the method does not change with what was built.
Numbering by render position would renumber every section each time one
was added, and would make the same section read differently in two
reports. Roman numerals, to match the outline the proposal is written in.

SEVEN SECTIONS, NOT NINE (branch 12). The outline shipped with nine
names, two of which no longer name a section of this report:

    Buildings & utilities   FOLDED INTO SITE OVERVIEW. What the report
                            has to say about what is built and what is
                            serviced is a handful of facts about the
                            property, not a section's worth of survey --
                            it belongs beside the parcel, the address and
                            the acreage on page one.
    Fencing & animals       INVESTIGATED AND DROPPED. There is no public
                            dataset that describes a parcel's fencing or
                            its stock, so a section under that heading
                            could only repeat the design's own choices
                            back at the reader, which is not inventory.

THE OUTLINE IS NOT THE MODULE LIST. fencing.py and the fencing step are
KSOP design modules and are untouched by this: what changed is what the
site data REPORT has a numbered section for. Soils & geology moves from
IX to VII because the two names ahead of it are gone, which is the one
renumbering the outline has taken and the reason the numeral lives here
rather than being counted at render time.
"""

SECTION_OUTLINE = (
    "Site overview",
    "Climate",
    "Landform",
    "Water & hydrology",
    "Access",
    "Trees & forestry",
    "Soils & geology",
)

_ROMAN = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII")


def section_number(name: str) -> str:
    """The Roman numeral of `name`'s place in SECTION_OUTLINE. Raises for a
    name that is not in the outline: a section the method does not have is
    a mistake to surface, not to number."""
    return _ROMAN[SECTION_OUTLINE.index(name)]
