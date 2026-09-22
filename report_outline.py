"""
report_outline.py

THE SITE DATA REPORT'S FIXED SECTION ORDER -- the Scale of Permanence,
as site-data-report-proposal.md lays it out -- and the numeral each
section carries because of its place in it.

    SECTION_OUTLINE          the nine section names, in order
    section_number("Climate") -> "II"

A SECTION'S NUMBER IS ITS PLACE IN THE OUTLINE, NOT ITS PLACE IN THE
RENDERED REPORT. Climate is II whether or not Site overview renders ahead
of it; a report that carries only the Climate section still says
"II · Climate", because the number tells the reader where the section
sits in the method, and the method does not change with what was built.
Numbering by render position would renumber every section each time one
was added, and would make the same section read differently in two
reports. Roman numerals, to match the outline the proposal is written in.
"""

SECTION_OUTLINE = (
    "Site overview",
    "Climate",
    "Landform",
    "Water & hydrology",
    "Access",
    "Trees & forestry",
    "Buildings & utilities",
    "Fencing & animals",
    "Soils & geology",
)

_ROMAN = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII")


def section_number(name: str) -> str:
    """The Roman numeral of `name`'s place in SECTION_OUTLINE. Raises for a
    name that is not in the outline: a section the method does not have is
    a mistake to surface, not to number."""
    return _ROMAN[SECTION_OUTLINE.index(name)]
