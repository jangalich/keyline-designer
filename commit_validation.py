"""
commit_validation.py

WHAT MAY BE COMMITTED, and what is merely RECORDED about it. The gate every
commit passes through on its way into the Design Document
(interactive-design-architecture-proposal.md section 2.5, as settled --
see THE CONTRACT below for where and why this departs from what section 2.5
proposed).

    check_commit(...)         -> CommitCheck, or raises CommitRejectedError
    exclusion_crossings(...)  -> what one committed geometry crosses
    annotate_crossings(...)   -> the FeatureCollection as it enters the
                                 document, each feature carrying its own

SEPARATE FROM step_orchestrator.py ON PURPOSE. This module answers a
geometric question about one feature set -- is it valid, where does it sit,
what does it cross -- and answers it as a pure function of the feature
collection plus the session's already-computed DEM, boundary and exclusion
result. It fetches nothing, writes nothing, and knows nothing about
documents, stores, caches, jobs or hooks. The orchestrator does the
sequencing; this does the judging, and the split is what lets the judging be
tested against real geometry without a session in the way.


THE CONTRACT
============

HARD GATES -- these REJECT the commit, naming the offending features:

  * BOUNDARY CONTAINMENT. Geometry outside the parcel boundary is rejected.
    It is the ONLY spatial hard gate. See
    step_registry.COMMIT_MUST_LIE_WITHIN for the full argument and for why
    it is a constant rather than a per-step field.

  * GEOMETRIC VALIDITY. Self-intersecting rings, degenerate or zero-area
    geometry, non-polygonal input, a zone covering no DEM cell centre, a
    zone whose cells are all nodata. wire_translation's rehydrator already
    detects every one of these and raises InboundGeometryError naming the
    defect; this module SURFACES that as a per-feature commit rejection.
    That is the whole reason the rehydrator is run here rather than after
    the write: a malformed ring reaching the document and blowing up later
    is a 500 nobody can act on, and the same ring caught here is a sentence
    telling the user which zone to redraw.

  * The shape declarations on the step's CommitContract: the layer name, the
    permitted geometry types, the feature-count bounds, provenance.

RECORDED, NOT REJECTED -- EXCLUSION CROSSINGS. A committed zone that
overlaps the hydric, canopy, slope, roads or setback mask is VALID. Which
gates it crosses, and by how much, is written into the document alongside
the feature.

WHY, so this is not re-litigated. The exclusion gates are advisory by
nature: a hydric rating is an inference off a survey polygon at survey
scale, and the person standing on the ground can see whether it is wet.
zoneGeometry.js states the rule outright -- "gates encoding physical
impossibility apply and gates rejecting weak candidates do not: off-parcel
is not their land, while canopy, hydric, slope, roads and setback are all
conditions of ground they own and may commit to knowingly" -- and
ProductionZonePanel.jsx makes the same argument about its own 80% ceiling:
having handed that judgment to the user, taking it back at the gate would
be incoherent. The server stays authoritative about what it can KNOW
(containment, validity) and advisory about what the user knows better.

This is a deliberate departure from proposal section 2.5, which had the
server re-validate a commit against the same eligibility masks the client
draws against. That posture is rejected; the shipped frontend's is the
settled one, and it is implemented here.


AGREEMENT WITH THE CLIENT
=========================

zoneGeometry.js's cautionsFor() computes exactly these crossings client-side
already, for the live caution list a user sees while drawing. The server's
record and the client's caution must not disagree about whether a crossing
EXISTS, so exclusion_crossings() below reuses its semantics point for point:

  * per gate, INDEPENDENTLY -- two crossings for a zone over canopy and
    slope, never one merged figure, because they are two different facts
    about the ground;
  * gates in exclusion_zones.LAYER_ORDER, the order the client receives
    them in;
  * a gate whose data_available is false is SKIPPED, not reported clear --
    "we did not look" and "it is clear" are different statements, and the
    panel's standing caveat is what says the first one;
  * a gate with no footprint on this parcel is skipped (the client's
    `!layer.geometry_wgs84` -- the same geometry, seen empty);
  * and CROSSING_MIN_ACRES, the same floor, applied the same way.

What differs, and must: the client measures in lon/lat with a cosine-
latitude scale, this measures in the DEM's own UTM metres. The acreages
therefore agree to within the projection difference rather than exactly, and
test_step_commit.py asserts that -- same gates, agreeing figures -- against
a direct port of cautionsFor() rather than against a hand-written
expectation.
"""

from dataclasses import dataclass, field
from typing import Optional

import step_registry
import wire_translation

# ======================================================================
# The two acreage floors
# ======================================================================

# THE SMALLEST CROSSING WORTH RECORDING, in acres. zoneGeometry.js's
# CAUTION_MIN_ACRES, at the same value and for the same reason, quoted from
# it because the two must not drift:
#
#   "A 5 m DEM cell is about 0.0062 acres, and the exclusion layers are raw
#   cell staircases being clipped against an arbitrary drawn ring. Below this
#   the result is the clip itself rather than a measurement of anything --
#   two geometries of different kinds disagreeing along an edge. 0.05 acres
#   is where a one-decimal figure stops being able to describe it: anything
#   under rounds to '0.0 acres', which states a measurement of zero over
#   ground the user is being warned about."
#
# The server has the same problem in a different frame -- a cell staircase
# against a ring that came home through two reprojections -- so it takes the
# same floor. A crossing the client dropped and the server recorded would put
# a caution in the document that the user was never shown.
CROSSING_MIN_ACRES = 0.05

# THE SMALLEST OVERHANG THAT REJECTS A COMMIT, in acres. Deliberately its own
# constant rather than a second use of the floor above, because it answers a
# different question and could move independently.
#
# WHY IT IS NOT ZERO. A zone the frontend clamped to the parcel was clipped
# in lon/lat, serialised, and reprojected into UTM here; along a boundary
# a thousand metres long, that round trip leaves a sliver of a few square
# metres on the outside that nobody drew and no gate should fire on.
# Rejecting it would make an unedited, correctly-clamped commit unlandable.
#
# WHY IT IS STILL SMALL. 0.05 acres is about 202 m^2 -- roughly eight DEM
# cells. Real off-parcel geometry (a ring drawn across the property line, a
# zone committed against the wrong boundary) is orders of magnitude above it.
# This is the noise floor of the measurement, not a tolerance for being a
# little bit off someone else's land.
BOUNDARY_OVERHANG_MIN_ACRES = 0.05


# ======================================================================
# Rejections
# ======================================================================
#
# EVERY REJECTION NAMES A FEATURE, because the frontend renders rejections
# PER FEATURE -- against the row and the shape on the map -- not as a banner
# over the panel. A message that says "the commit is invalid" leaves the user
# to find which of eleven zones is the problem by deleting them one at a
# time. The `code` is the stable half of the pair and the `reason` is prose
# that will be reworded, the same split exclusion_zones._wire_layers() uses
# for type/label.

REJECT_NOT_A_COLLECTION = "not_a_feature_collection"
REJECT_TOO_FEW = "too_few_features"
REJECT_TOO_MANY = "too_many_features"
REJECT_NOT_A_FEATURE = "not_a_feature"
REJECT_MISSING_ID = "missing_feature_id"
REJECT_DUPLICATE_ID = "duplicate_feature_id"
REJECT_WRONG_LAYER = "wrong_layer"
REJECT_WRONG_GEOMETRY_TYPE = "wrong_geometry_type"
REJECT_MISSING_PROVENANCE = "missing_provenance"
REJECT_UNKNOWN_PROVENANCE = "unknown_provenance"
REJECT_ORPHAN_PROVENANCE = "provenance_names_no_feature"
REJECT_INVALID_GEOMETRY = "invalid_geometry"
REJECT_OUTSIDE_BOUNDARY = "outside_boundary"


@dataclass(frozen=True)
class FeatureRejection:
    """
    One reason one feature (or the collection itself) cannot be committed.

    feature_id is None for a defect of the collection rather than of a
    feature in it -- too many features, a payload that is not a
    FeatureCollection at all. Those are the only two kinds, and a client
    rendering per-feature messages can put a null-id rejection at the top of
    the panel without having to guess which of them it is looking at.
    """

    feature_id: Optional[str]
    code: str
    reason: str


class CommitRejectedError(Exception):
    """
    A commit that does not meet its step's contract. Carries EVERY rejection
    found, not the first.

    ALL OF THEM, ON PURPOSE. A gate that stops at the first bad feature turns
    a commit with three problems into three round trips, each of which
    re-renders the panel and each of which the user has to interpret alone.
    The checks are cheap next to the rehydration they gate, and the
    rehydration itself is per-feature, so collecting the full set costs one
    pass.

    NOTHING IS WRITTEN when this raises. The document write happens after
    this gate and only after it, so a rejected commit leaves the step exactly
    as it was -- which is what makes it safe for a client to retry with a
    corrected feature set and the same base_revision.
    """

    def __init__(self, step_id: str, rejections):
        self.step_id = step_id
        self.rejections = tuple(rejections)
        named = ", ".join(
            f"{r.feature_id or '<collection>'}: {r.code}" for r in self.rejections
        )
        super().__init__(
            f"commit to step '{step_id}' rejected -- "
            f"{len(self.rejections)} problem(s): {named}"
        )

    def as_payload(self) -> dict:
        """
        The wire shape. `rejections` is a LIST, in the order the features
        arrived, so a client can walk its own feature list against it.
        """
        return {
            "error": f"This {self.step_id} commit could not be saved.",
            "rejections": [
                {
                    "feature_id": rejection.feature_id,
                    "code": rejection.code,
                    "reason": rejection.reason,
                }
                for rejection in self.rejections
            ],
        }


@dataclass
class CommitCheck:
    """
    What a passing commit yields: the internal shape every downstream
    override takes, and the internal ids that were used to build it.

    `rehydrated` is in FEATURE ORDER, one entry per committed feature, so it
    lines up positionally with the collection and with `zone_ids`. The
    orchestrator hands it straight to the post-commit hooks and caches it for
    the SOURCE_COMMITTED resolver -- rehydration is not cheap (a
    rasterization and a morphological opening per zone) and doing it once per
    commit rather than once per downstream read is the difference between a
    warm session and a slow one.
    """

    rehydrated: list = field(default_factory=list)
    internal_ids: list = field(default_factory=list)


# ======================================================================
# Internal ids for a commit
# ======================================================================


def internal_ids_for(features: list, provenance: dict) -> list:
    """
    One INTERNAL id per committed feature, in order.

    A SELECTED GENERATED FEATURE keeps its own: its wire id is
    "production-area-<n>" and n is the patch id the pipeline gave it, which
    is what makes a rehydrated patch line up with anything already recorded
    against that id (water's served_production_area_ids, for one).
    wire_translation.internal_zone_id() is the one place that spelling is
    parsed.

    A USER-DRAWN FEATURE has no pipeline id, and the rehydrator refuses to
    invent one -- an invented id can collide with a generated zone's in the
    same commit and silently merge their served-area accounting. So one is
    ALLOCATED here, above every id the same commit already uses, in feature
    order.

    DETERMINISTIC IN THE COMMITTED COLLECTION, which is the property that
    matters: this function is called once when the commit is validated and
    again every time the SOURCE_COMMITTED resolver rehydrates that commit for
    a downstream step, and both calls see the same stored collection in the
    same order, so both produce the same ids. It is NOT stable across
    re-commits -- a drawn zone added at the front of a later commit shifts
    the allocation -- and it must not be read as an identity that outlives a
    commit. The wire id is that identity; this is the internal handle the
    pipeline's own dicts use.
    """
    # Defensive on the feature shape, because check_commit() calls this
    # BEFORE it has raised on a malformed collection -- a positional id list
    # has to line up with the features list even when one of them is junk, or
    # the report the caller is about to get is the wrong exception.
    wire_ids = [
        feature.get("id") if isinstance(feature, dict) else None for feature in features
    ]
    parsed = [wire_translation.internal_zone_id(wire_id) for wire_id in wire_ids]
    next_id = max((zone_id for zone_id in parsed if zone_id is not None), default=-1) + 1

    ids = []
    for index, zone_id in enumerate(parsed):
        if zone_id is None:
            zone_id = next_id
            next_id += 1
        elif provenance.get(wire_ids[index]) == "user_added":
            # A DRAWN ZONE WEARING A GENERATED ZONE'S ID. Its wire id parses,
            # so the branch above would hand it that patch's number and merge
            # the two in every downstream dict keyed by id. The provenance is
            # the user's own statement that this is not that zone, so it is
            # believed and a fresh id is allocated.
            zone_id = next_id
            next_id += 1
        ids.append(zone_id)
    return ids


# ======================================================================
# Exclusion crossings -- recorded, never rejected
# ======================================================================


def exclusion_crossings(polygon_utm, exclusion_result: dict) -> list:
    """
    What ONE committed geometry crosses, per gate, above the floor.

    `polygon_utm` is the rehydrated patch's own polygon in the DEM's CRS;
    `exclusion_result` is the session's cached identify_exclusion_zones()
    result -- the same object the generate ran against, so the gates a
    crossing is measured against are the gates the proposals were computed
    against.

    Returns, in exclusion_zones.LAYER_ORDER:

        [{"type": "hydric", "label": "hydric soil", "acres": 1.23}, ...]

    `type` is the stable identifier and `label` is the gate's own display
    prose, taken VERBATIM off the exclusion result's wire block rather than
    reworded here -- it states the test that was applied ("slope above
    20.0%"), which is what someone overriding an exclusion is entitled to
    read, and it is the same string the client already showed them.

    EMPTY IS THE COMMON, CORRECT CASE. A selected generated zone crosses
    nothing by construction (it is an opening of ground that already cleared
    every gate -- zoneGeometry.js asserts exactly that as a DEV invariant),
    so an empty list here is a zone in the clear, never a check that did not
    run. A gate that could not be checked at all is skipped for a different
    reason and says so through data_available, which travels to the client on
    the payload's own exclusion_layers.

    NO CENTROID. cautionsFor() also returns `at`, the largest crossing
    piece's centroid, because it has to put a marker on a map. A stored
    record does not, and a coordinate written into the document would be a
    display decision frozen into a decision record.
    """
    if polygon_utm is None or polygon_utm.is_empty:
        return []

    layers = exclusion_result["layers"]
    crossings = []
    # The wire block is iterated rather than LAYER_ORDER directly: it is
    # already in LAYER_ORDER, it carries the label and the availability flag
    # in the exact form the client received them, and reading the two halves
    # off one source is what stops the server's record and the client's
    # caution disagreeing about a gate's identity.
    for wire_layer in exclusion_result["wire"]["layers"]:
        name = wire_layer["type"]
        if not wire_layer["data_available"]:
            # "We did not look" is not "it is clear". Skipped in silence
            # here exactly as cautionsFor() skips it, because the honest
            # statement about an unchecked gate is the step-wide caveat the
            # panel already renders, not a per-feature record.
            continue
        gate_polygon = layers[name]["polygon_utm"]
        if gate_polygon.is_empty:
            continue
        hit = polygon_utm.intersection(gate_polygon)
        if hit.is_empty:
            continue
        acres = _acres(hit)
        if acres < CROSSING_MIN_ACRES:
            # See CROSSING_MIN_ACRES. Dropped rather than recorded with a
            # hedge, and dropped at the same threshold the client drops it,
            # so the document never carries a caution the user was not shown.
            continue
        crossings.append(
            {"type": name, "label": wire_layer["label"], "acres": round(acres, 2)}
        )
    return crossings


def annotate_crossings(features: list, rehydrated: list, exclusion_result: dict) -> dict:
    """
    The FeatureCollection as it enters the Design Document: every feature
    exactly as it arrived, plus its own crossings under
    properties.exclusion_crossings.

    ALONGSIDE THE FEATURE, not in a parallel map keyed by id. A committed
    feature and what it crosses are one fact, and splitting them puts the
    burden of keeping two lists aligned on every later reader -- including
    the one who deletes a feature from a re-commit and forgets the other
    half. It also needs no design_document schema change: the entry's shape
    is status/revision/features/provenance/inputs and this rides inside
    features.

    ALWAYS PRESENT, never omitted when empty. `[]` says "checked, crosses
    nothing"; an absent key would say "this commit predates crossings being
    recorded", and those must stay distinguishable.

    The feature is otherwise UNTOUCHED -- no geometry rewrite, no scoring
    field invented for a drawn zone, no confidence note synthesised. What the
    user committed is what the document holds.
    """
    annotated = []
    for feature, patch in zip(features, rehydrated):
        properties = dict(feature.get("properties") or {})
        properties["exclusion_crossings"] = exclusion_crossings(
            patch["polygon_utm"], exclusion_result
        )
        annotated.append({**feature, "properties": properties})
    return {"type": "FeatureCollection", "features": annotated}


def _acres(geometry) -> float:
    from raster_grid import SQUARE_METERS_PER_ACRE

    return float(geometry.area / SQUARE_METERS_PER_ACRE)


# ======================================================================
# The gate
# ======================================================================


def check_commit(
    definition,
    features: dict,
    provenance: dict,
    dem: dict,
    boundary_polygon_utm,
) -> CommitCheck:
    """
    A proposed commit against its step's CommitContract. Returns the
    rehydrated internal shape on a pass; raises CommitRejectedError carrying
    every problem, per feature, on a failure.

    THE ORDER OF THE CHECKS IS THE ORDER OF THEIR COST, cheapest first, and
    also the order of how specific the resulting message is. Collection shape
    and count, then per-feature shape, provenance and geometry type -- all
    dictionary work -- and only then the rehydration, which rasterises and
    opens a polygon per zone. A feature that already failed a cheap check is
    NOT rehydrated: the expensive answer would tell the user nothing the
    cheap one did not.

    `dem` and `boundary_polygon_utm` come off the session context and are
    passed explicitly rather than as a context object, for the reason
    wire_translation.py gives for the same choice: a function that reaches
    for a whole context cannot be called with one layer's values in hand.
    """
    contract = definition.commit_contract
    rejections = []

    if not isinstance(provenance, dict):
        raise CommitRejectedError(
            definition.step_id,
            [
                FeatureRejection(
                    None,
                    REJECT_MISSING_PROVENANCE,
                    "Provenance must be a {feature id -> classification} map, "
                    f"got {type(provenance).__name__}.",
                )
            ],
        )

    if not isinstance(features, dict) or features.get("type") != "FeatureCollection":
        raise CommitRejectedError(
            definition.step_id,
            [
                FeatureRejection(
                    None,
                    REJECT_NOT_A_COLLECTION,
                    "A commit must be a GeoJSON FeatureCollection, got "
                    f"{type(features).__name__}"
                    + (
                        f" with type {features.get('type')!r}"
                        if isinstance(features, dict)
                        else ""
                    )
                    + ".",
                )
            ],
        )
    feature_list = features.get("features")
    if not isinstance(feature_list, list):
        raise CommitRejectedError(
            definition.step_id,
            [
                FeatureRejection(
                    None,
                    REJECT_NOT_A_COLLECTION,
                    "A FeatureCollection's 'features' must be a list.",
                )
            ],
        )

    # THE COUNT. min_features is 0 for every step, and an empty commit is a
    # decision rather than an omission -- so this is not a "did you forget to
    # select something" check and must never become one.
    if len(feature_list) < contract.min_features:
        rejections.append(
            FeatureRejection(
                None,
                REJECT_TOO_FEW,
                f"This step needs at least {contract.min_features} feature(s); "
                f"{len(feature_list)} were committed.",
            )
        )
    if contract.max_features is not None and len(feature_list) > contract.max_features:
        rejections.append(
            FeatureRejection(
                None,
                REJECT_TOO_MANY,
                f"This step takes at most {contract.max_features} feature(s); "
                f"{len(feature_list)} were committed.",
            )
        )

    # --- per feature, the cheap checks --------------------------------
    seen_ids = set()
    checkable = []  # (index, feature_id) that survived to rehydration
    for index, feature in enumerate(feature_list):
        where = f"feature at index {index}"
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            rejections.append(
                FeatureRejection(
                    None, REJECT_NOT_A_FEATURE, f"{where} is not a GeoJSON Feature."
                )
            )
            continue

        feature_id = feature.get("id")
        if not feature_id or not isinstance(feature_id, str):
            rejections.append(
                FeatureRejection(
                    None,
                    REJECT_MISSING_ID,
                    f"{where} has no string id. Every committed feature needs "
                    "one: it is what a rejection, a selection and a later "
                    "reopen all name it by.",
                )
            )
            continue
        if feature_id in seen_ids:
            rejections.append(
                FeatureRejection(
                    feature_id,
                    REJECT_DUPLICATE_ID,
                    f"Two committed features carry the id {feature_id!r}.",
                )
            )
            continue
        seen_ids.add(feature_id)

        properties = feature.get("properties") or {}
        rejected_here = False

        if properties.get("layer") not in contract.layers:
            # MEMBERSHIP, not equality -- a step may commit more than one
            # layer. The water step commits both survey_zone_embankment and
            # survey_zone_excavated, because a survey zone's TYPE is carried
            # by its layer and a selection spans both types freely. The
            # message lists what is accepted rather than naming one, so a
            # client sending a member footprint or a dropped zone is told
            # which layers ARE committable instead of being told it sent the
            # wrong one of two.
            accepted = " or ".join(repr(layer) for layer in contract.layers)
            rejections.append(
                FeatureRejection(
                    feature_id,
                    REJECT_WRONG_LAYER,
                    f"This step commits {accepted} features; this one "
                    f"carries layer {properties.get('layer')!r}.",
                )
            )
            rejected_here = True

        geometry_type = (feature.get("geometry") or {}).get("type")
        if geometry_type not in contract.geometry_types:
            rejections.append(
                FeatureRejection(
                    feature_id,
                    REJECT_WRONG_GEOMETRY_TYPE,
                    f"This step commits {' or '.join(contract.geometry_types)}; "
                    f"this one is {geometry_type!r}.",
                )
            )
            rejected_here = True

        if contract.requires_provenance:
            classification = provenance.get(feature_id)
            if classification is None:
                rejections.append(
                    FeatureRejection(
                        feature_id,
                        REJECT_MISSING_PROVENANCE,
                        "This feature has no provenance. Every committed "
                        "feature is either a generated candidate the user "
                        "selected or a shape they drew, and which one it is "
                        "is not derivable from the feature.",
                    )
                )
                rejected_here = True
            elif classification not in accepted_provenance_values():
                rejections.append(
                    FeatureRejection(
                        feature_id,
                        REJECT_UNKNOWN_PROVENANCE,
                        _provenance_reason(classification),
                    )
                )
                rejected_here = True

        if not rejected_here:
            checkable.append(index)

    # PROVENANCE NAMING NO FEATURE. Not pedantry: it is how a client that
    # deleted a zone from its selection but not from its provenance map finds
    # out, instead of committing a map that describes a feature set it is no
    # longer sending.
    for provenance_id in provenance:
        if provenance_id not in seen_ids:
            rejections.append(
                FeatureRejection(
                    provenance_id,
                    REJECT_ORPHAN_PROVENANCE,
                    f"Provenance names {provenance_id!r}, which is not in the "
                    "committed feature set.",
                )
            )

    # --- per feature, the expensive checks ----------------------------
    internal_ids = internal_ids_for(feature_list, provenance)
    rehydrate = step_registry.resolve(contract.rehydrate)
    rehydrated = [None] * len(feature_list)

    for index in checkable:
        feature = feature_list[index]
        feature_id = feature["id"]
        kwargs = {}
        if contract.internal_id_parameter:
            kwargs[contract.internal_id_parameter] = [internal_ids[index]]
        try:
            # ONE FEATURE AT A TIME, through the step's own declared
            # collection rehydrator. That translator fails the WHOLE call on
            # one bad feature by contract -- correctly, for its own callers,
            # since a partial list is a commit nobody made -- but this gate
            # needs to name every bad feature rather than the first, and a
            # one-feature collection is how it asks the same question per
            # feature without a second implementation of the translation.
            patch = rehydrate(
                {"type": "FeatureCollection", "features": [feature]}, dem, **kwargs
            )[0]
        except wire_translation.InboundGeometryError as exc:
            # THE VALIDITY GATE, AND WHY IT IS A REJECTION RATHER THAN A 500.
            # The rehydrator already found the defect and named it -- a
            # self-intersecting ring, a collinear sliver, a zone covering no
            # cell centre. Letting that exception escape would turn a
            # correctable drawing mistake into a server error with a
            # traceback; caught here it is a sentence naming the zone.
            rejections.append(
                FeatureRejection(feature_id, REJECT_INVALID_GEOMETRY, str(exc))
            )
            continue

        # BOUNDARY CONTAINMENT. The one spatial hard gate. Measured as the
        # part of the committed geometry lying OUTSIDE the parcel, because
        # that is the quantity the user has to act on -- "1.7 acres of this
        # zone is off your parcel" -- where a boolean `within` test says only
        # that something is wrong somewhere.
        overhang_acres = _acres(patch["polygon_utm"].difference(boundary_polygon_utm))
        if overhang_acres >= BOUNDARY_OVERHANG_MIN_ACRES:
            rejections.append(
                FeatureRejection(
                    feature_id,
                    REJECT_OUTSIDE_BOUNDARY,
                    f"{overhang_acres:.2f} acres of this feature lie outside "
                    "the parcel boundary. The parcel is the one hard limit on "
                    "where a feature can go -- everything else this step "
                    "checks is advisory and is recorded rather than refused.",
                )
            )
            continue

        rehydrated[index] = patch

    if rejections:
        raise CommitRejectedError(definition.step_id, rejections)

    return CommitCheck(rehydrated=rehydrated, internal_ids=internal_ids)


def accepted_provenance_values() -> tuple:
    """
    The accepted provenance vocabulary, read from design_document rather than
    restated -- that module owns it and the document is what enforces it. Imported inside the function so this module stays importable
    without it in the one place that matters -- it is a hard dependency
    either way, and the local import keeps the module's import list honest
    about what it needs at module scope.
    """
    from design_document import PROVENANCE_VALUES

    return PROVENANCE_VALUES


def _provenance_reason(classification) -> str:
    """
    The message for a provenance value the document will not accept, with the
    one historical value called out by name.

    "user_modified" USED TO BE ACCEPTED and is now rejected outright, so a
    client still sending it gets told what changed rather than a bare list of
    valid values. Nothing in this system can produce a user-modified feature:
    generated candidates are SELECT-ONLY at every step, with no vertex
    editing anywhere, so the value described a case that could not arise. It
    was removed from design_document.PROVENANCE_VALUES rather than left
    accepted-but-unreachable -- see the comment there.
    """
    accepted = ", ".join(repr(value) for value in accepted_provenance_values())
    if classification == "user_modified":
        return (
            "'user_modified' is not a provenance this system accepts. Generated "
            "candidates are select-only at every step -- there is no vertex "
            "editing anywhere -- so a modified candidate cannot arise. A "
            "feature is either a generated candidate that was selected "
            f"('generated') or a shape the user drew ('user_added'). Accepted: "
            f"{accepted}."
        )
    return f"Provenance {classification!r} is not one of {accepted}."
