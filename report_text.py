"""
report_text.py

THE WORDS OF THE SITE DATA REPORT: the one place a caption or a summary
line is assembled from. Every section builder composes its prose from the
helpers here, so a change to how captions work is one change, not eight.

A PART LIST is what the templates set (components/caption.html,
components/summary.html): a string is prose, set in the prose face; a
mapping {value, kind?} is data, set in the data face with tabular figures.

THE DATA RULE, MADE CHECKABLE. Every data part is a `Value` -- a dict
subclass the templates read as the same mapping -- and the formatters below
are the only code that make one from a number. A formatter takes the
MEASUREMENT OR THE CONSTANT, never a typed string: `feet(15.0)` is not how a
caption says fifteen metres, `feet(FRONTAGE_TOLERANCE_METERS)` is. So a
number that reaches a caption either came through a formatter from data or
a named constant, or it sits in a prose string where test_report_text.py
finds it (`digits_in_prose`). The test also reads every builder's caption
and summary functions and fails on a formatter called with a literal
number (`literal_calls`). What the check cannot see is a number computed
from a typed literal before it reaches a formatter -- `feet(x * 3)` -- which
is why the same lint fails on any numeric literal other than 0 and 1 in
those functions: a threshold that chooses a word is a named constant too.

THE UNIT SITS INSIDE THE SPAN. "49 ft", "1.6 acres", "38%": the figure and
its unit are one datum, set together. A count is the exception -- "6 of 27
patches": the number is data, the noun is prose.

SENTENCES. A builder returns optional clauses, each a small function that
returns a part list or None; `sentences(*clauses)` drops the empty ones and
puts exactly one space between the rest -- no leading or trailing space, so
no caption ends in one.

TERMS. A figure's name can carry a digit that is not a measurement --
FEMA's "1%-annual-chance" floodplain. Such a term is spelled once, in
TERMS, and nowhere else; digits_in_prose accepts it and nothing else.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence, Union

METERS_PER_FOOT = 0.3048
MM_PER_INCH = 25.4
SQUARE_METERS_PER_ACRE = 4046.8564224
MINUS = "−"
EN_DASH = "–"

# Source vocabulary whose NAME carries a digit. The only digits a prose
# string in a caption or summary may hold.
TERMS = (
    "1%-annual-chance",
    "0.2%-annual-chance",
    "100-year",
    "Atlas 14",
    "Atlas 15",
    "Atlas 2",
)
# ONE PLAIN WORD PER SURVEY UNIT, the same in every section: a soil map
# unit glossed where it is read (Water, Soils), singular and plural. The
# Trees section's "wooded patch" is its 30 m closure block; no other
# section names a cell-based unit in its prose.
MAP_UNIT = "an area of one kind of soil"
MAP_UNITS = "areas of one kind of soil"
# A share of a whole, in percent: what a share column sums to.
WHOLE_PERCENT = 100.0


class Value(dict):
    """A data part. Built only by the functions in this module."""


Part = Union[str, Value]


# ======================================================================
# Data parts
# ======================================================================


def text(value: str) -> Value:
    """A datum that is a NAME read from the data -- a soil series, a forest
    type group, a year the source record states -- set in the data face.
    Not for a number: the formatters below are."""
    return Value(value=str(value))


def word(value: str) -> Value:
    """A datum set in the PROSE face inside a numeric slot -- 'None', 'no
    data' -- the kind the data table and key figures already honour."""
    return Value(value=str(value), kind="word")


def bound(value: str) -> Value:
    """A limit, not a measurement -- '> 72' -- set muted."""
    return Value(value=str(value), kind="bound")


# ======================================================================
# Formatters: a measurement or a constant in, a Value with its unit out
# ======================================================================


def _grouped(value: float, places: int) -> str:
    return f"{value:,.{places}f}"


def number(value: float, places: int = 0) -> Value:
    """A bare figure: a count, a factor, a ratio."""
    return Value(value=_grouped(value, places))


def feet(meters: float) -> Value:
    """A length measured in metres, set in whole feet: '49 ft'."""
    return Value(value=f"{_grouped(round(meters / METERS_PER_FOOT), 0)} ft")


def feet_of(feet_value: float, places: int = 0) -> Value:
    """A length already in feet: '1,263 ft'; a small one to a tenth, '1.1 ft'."""
    return Value(value=f"{_grouped(round(feet_value, places) if places else round(feet_value), places)} ft")


def feet_range(low_ft: float, high_ft: float) -> Value:
    """A range already in feet: '100–300 ft'."""
    return Value(value=f"{_grouped(low_ft, 0)}{EN_DASH}{_grouped(high_ft, 0)} ft")


def meters(value: float) -> Value:
    """A length in metres, where the metre is the source's own unit."""
    return Value(value=f"{value:g} m")


def miles(value: float, places: int = 0) -> Value:
    return Value(value=f"{_grouped(value, places)} mi")


def inches(mm: float, places: int = 1) -> Value:
    """A depth measured in millimetres, set in inches: '1.9 in'."""
    return Value(value=f"{_grouped(mm / MM_PER_INCH, places)} in")


def inches_of(inches_value: float, places: int = 1) -> Value:
    return Value(value=f"{_grouped(inches_value, places)} in")


def acres(value: float, places: int = 1) -> Value:
    """An area already in acres, one decimal, the unit inside: '1.6 acres'.
    A value that is not zero but rounds below a tenth reads '<0.1 acres'.
    A catchment's hundreds take `places=0`: '532 acres'."""
    if places == 1 and 0 < value < 0.05:
        return Value(value="<0.1 acres")
    return Value(value=f"{_grouped(value, places)} acres")


def acres_adjective(value: float) -> Value:
    """An area in acres as a modifier: 'this 13.2-acre parcel'."""
    return Value(value=f"{_grouped(value, 1)}-acre")


def acres_of_m2(square_meters: float) -> Value:
    return acres(square_meters / SQUARE_METERS_PER_ACRE)


def percent(value: float, places: int = 0) -> Value:
    """A share already in percent: '38%'."""
    return Value(value=f"{value:.{places}f}%")


def percent_range(low: float, high: float) -> Value:
    """A range of shares already in percent: '25–27%'."""
    return Value(value=f"{low:.0f}{EN_DASH}{high:.0f}%")


def centimeters(value: float) -> Value:
    """A depth in the source's own centimetres: '150 cm'."""
    return Value(value=f"{value:g} cm")


def share(fraction: float, places: int = 0) -> Value:
    """A share given as a fraction of one: 0.38 -> '38%'."""
    return percent(fraction * 100.0, places)


def times(factor) -> Value:
    """A multiplier the data gives -- a chart's vertical exaggeration: '3×'."""
    return Value(value=f"{factor}\u00d7")


def year(value) -> Value:
    """A year the data states -- an acquisition, a record's end -- set as a
    name in the prose face: a date, not a measurement."""
    return Value(value=str(value), kind="word")


def scale(denominator: int) -> Value:
    """A map scale from the source's own figure: '1:24,000'."""
    return Value(value=f"1:{denominator:,}")


# ======================================================================
# Bare figures for table cells and key figures: the same rounding as the
# formatters above, without the unit (a column's header carries it)
# ======================================================================


def whole_text(value: float) -> str:
    """'1,234': a figure rounded to a whole number."""
    return _grouped(round(value), 0)


def feet_text(meters: float) -> str:
    """A length in metres as whole feet, without the unit."""
    return whole_text(meters / METERS_PER_FOOT)


def percent_text(value: float) -> str:
    """A share already in percent: '38%'."""
    return percent(value)["value"]


# ======================================================================
# Words
# ======================================================================


def plural(count: int, noun: str, plural_form: Optional[str] = None) -> str:
    """The noun, singular or plural for `count` -- the noun only."""
    return noun if count == 1 else (plural_form or noun + "s")


def agree(count: int, singular: str, plural_form: str) -> str:
    """A verb or pronoun that agrees with `count`: agree(n, 'is', 'are')."""
    return singular if count == 1 else plural_form


def count(value: int, noun: str, plural_form: Optional[str] = None) -> list:
    """'6 patches': the number as data, the noun as prose."""
    return [number(value), " " + plural(value, noun, plural_form)]


def lower_first(value: str) -> str:
    return value[:1].lower() + value[1:] if value else value


def upper_first(value: str) -> str:
    return value[:1].upper() + value[1:] if value else value


def series(items: Sequence, conjunction: str = "and") -> list:
    """'a, b and c' as a PART LIST. An item is a string, a Value or a part
    list, so a data value inside a list stays data. Empty items drop."""
    items = [i for i in items if i not in (None, "", [])]
    parts: list = []
    for index, item in enumerate(items):
        if index:
            parts.append(f" {conjunction} " if index == len(items) - 1 else ", ")
        parts += item if isinstance(item, list) else [item]
    return merge(parts)


def series_text(items: Sequence[str], conjunction: str = "and") -> str:
    """series() for plain strings, as one string."""
    return "".join(series(list(items), conjunction))


# ======================================================================
# Assembly
# ======================================================================


def merge(parts: Iterable[Part]) -> list:
    """Adjacent prose strings joined, empty strings dropped."""
    out: list = []
    for part in parts:
        if isinstance(part, str):
            if not part:
                continue
            if out and isinstance(out[-1], str):
                out[-1] += part
                continue
        out.append(part)
    return out


def _trim(parts: list) -> list:
    parts = merge(parts)
    if parts and isinstance(parts[0], str):
        parts[0] = parts[0].lstrip()
    if parts and isinstance(parts[-1], str):
        parts[-1] = parts[-1].rstrip()
    return [p for p in parts if p != ""]


def clause(*parts) -> list:
    """A part list from its pieces: strings, Values, and part lists, which
    are spliced in."""
    out: list = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, list):
            out += part
        else:
            out.append(part)
    return merge(out)


def sentences(*clauses) -> list:
    """The part list of the non-empty clauses, one space between each and
    none at either end. A clause is a part list or None."""
    out: list = []
    for item in clauses:
        if not item:
            continue
        trimmed = _trim(list(item))
        if not trimmed:
            continue
        if out:
            out.append(" ")
        out += trimmed
    return merge(out)


# ======================================================================
# The check (test_report_text.py and every section's test use these)
# ======================================================================

_DIGIT = re.compile(r"\d")


def flatten(parts: Sequence) -> str:
    return "".join(p if isinstance(p, str) else str(p["value"]) for p in parts or [])


def digits_in_prose(parts: Sequence) -> list:
    """The prose strings in a part list that carry a digit outside TERMS --
    a number typed into a sentence rather than formatted from data."""
    found = []
    for part in parts or []:
        if isinstance(part, str):
            stripped = part
            for term in TERMS:
                stripped = stripped.replace(term, "")
            if _DIGIT.search(stripped):
                found.append(part)
    return found


def untracked_values(parts: Sequence) -> list:
    """Data parts that did not come from this module."""
    return [p for p in parts or [] if not isinstance(p, str) and not isinstance(p, Value)]
