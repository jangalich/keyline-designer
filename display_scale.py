"""
display_scale.py

THE KSOP DISPLAY SCALE: 0-100, FOR READING ONLY.

WHAT THIS IS. One function and one constant. Every KSOP step grades zones
on its own internal scale -- water's suitability is a 0-1 weighted
overlay, landform's ceiling score is already 0-100 -- and a reader
clicking from one step's panel to the next should not have to re-learn
the units. So the PRESENTATION layer states one scale, 0-100, and each
step converts AT THE POINT IT RENDERS. Water is the first step through
here; the rest follow separately, reusing this module rather than
copying the multiply.

WHAT THIS IS NOT, and the distinction is the whole point:

  * NOT A RESCALE OF THE SYSTEM. Scores, thresholds, weights, the seed
    minimum, the ranking composite and every stored field stay on their
    own internal scale, untouched. This module adds a REPRESENTATION of
    a value; it never produces one that is scored, compared, ranked,
    thresholded or stored. A converted number must never flow back into
    a computation.

  * NOT A NORMALIZATION AGAINST THE PARCEL'S OBSERVED MAXIMUM. Making a
    parcel's best cell read 100 was considered and REJECTED. That
    reference is computed from the parcel's own cells, so it moves
    whenever the boundary moves: the same ground would score
    differently under a redrawn boundary, which is exactly the class of
    bug the window-referenced TWI work eliminated (see
    water_survey_areas.py's boundary-dependence audit). It would also
    destroy cross-property comparability -- a 100 on poor ground and a
    100 on excellent ground would read identically, and a reader could
    not tell a good parcel from a bad one. The RELATIVE reading is
    served instead by the scales block's `parcel_observed_max`, which
    is carried beside the value and converted through this same helper,
    so "44 of 87 attainable here" says both things without either
    number lying.

ONE CONVERSION POINT, ENFORCED. Every site that shows a 0-1 score on the
0-100 display scale calls to_display_scale(); nothing else multiplies by
DISPLAY_SCALE_MAX. Two independent multipliers is how a display scale
silently becomes two different scales -- one call site gains a round(),
another gains a clamp, and the panel and the report disagree about the
same zone. test_display_scale.py asserts this AST-style across the
modules that render, and the frontend does no multiplying at all: it
renders the server's converted number as sent.

AMBIGUITY IS WORSE THAN EITHER SCALE. A "44" that might be 0.44 and might
be 44/100 is less useful than either one alone, so every converted value
carries its scale at its point of use: a panel row says so in its `unit`
(DISPLAY_SCALE_UNIT), prose spells it out ("44/100"), and the stored
fields keep their 0-1 names and values unchanged so no consumer can
mistake one for the other.
"""

DISPLAY_SCALE_MIN = 0
DISPLAY_SCALE_MAX = 100
"""The presentation scale's endpoints. INTEGERS, because the scale is a
whole-number reading: 0-100 with a decimal point is a 0-1 fraction
wearing a costume."""

DISPLAY_SCALE_UNIT = "/100"
"""What a converted value's `unit` field says, so a panel row is
unambiguous without its renderer knowing which row it is drawing. Shared
with the frontend, which prints it and never computes with it."""


def to_display_scale(value):
    """
    A 0-1 internal score as its 0-100 DISPLAY reading -- a whole number.

    None passes through as None. "Not measured" is a value this pipeline
    carries honestly end to end, and a converted 0 would report an
    unmeasured thing as a measured floor.

    ROUNDING: round(value * 100), which is Python's round-half-to-EVEN on
    the product. 0.125 -> 12 and 0.375 -> 38; 0.445 -> 44 and 0.455 ->
    46. Stated because a rounding rule nobody wrote down is a rounding
    rule that changes. Half-to-even rather than half-up because it is
    what the standard library does and a second rule here would be one
    more thing to keep in step; the choice does not matter to a reader
    of a whole-number grade, and the ONLY thing that matters is that
    every site rounds the same way, which is what one helper buys.

    NO CLAMP. A value outside 0-1 is a scoring defect and must surface as
    a visibly wrong grade, not be quietly folded to 0 or 100 by the
    display layer.
    """
    if value is None:
        return None
    return int(round(float(value) * DISPLAY_SCALE_MAX))
