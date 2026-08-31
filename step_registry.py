"""
step_registry.py

THE STEP REGISTRY -- the interactive Layer 2
(interactive-design-architecture-proposal.md section 2.3), the declarative
sibling of pipeline_context.build_pipeline_context() and, deliberately, its
architectural equal rather than its wrapper.

WHAT THIS MODULE IS. A table. Every entry names one KSOP step and declares,
as DATA:

    consumes         what upstream values the step's generate needs, where
                     each one comes from, and which OVERRIDE parameter of
                     the entry point it is forwarded into
    generate         the existing entry point to call
    produces         the context fields the step contributes
    commit_contract  what a valid commit looks like
    user_inputs      extra parameters collected at this step

WHY DATA AND NOT CODE, WHICH IS THE WHOLE POINT. step_orchestrator.py READS
these declarations; it does not know that landform means production areas,
that exclusion_zones is the mask, or that identify_optimized_production_
areas() is the function. Adding the water step is one entry in the table
below -- not a branch in the orchestrator, not a second generate function,
not a new endpoint. The day that stops being true, this module has failed at
the one job it exists for.

THE `consumes` EDGES ARE THE INVALIDATION EDGES. Section 2.3 states it and
section 4 depends on it: the same declarations the orchestrator assembles a
generate from are the graph the commit cascade walks to decide what a
re-commit invalidates. They are written once, here, so the two can never
disagree -- which is exactly the failure mode a hand-maintained "downstream
of" list has. dependents_of() below reads that graph; the cascade itself is
B5b's.

ORDER COMES FROM design_document.STEP_ORDER, never from this file. A second
ordered step list would be a second answer to "what comes after landform",
and the document's is the one the frontend, the cascade and the reset all
already read. registered_steps() filters that constant; it does not restate
it.

PARTIALLY POPULATED, ON PURPOSE. ONE entry today: landform. water, roads,
trees, structures and fencing are named in STEP_ORDER and absent here, and
the difference is meaningful rather than an oversight -- registered_steps()
returns what can actually be generated, and asking for an unregistered step
raises with the list of what is registered. The parity test against
build_pipeline_context() belongs at the end of stage 3, when all six exist
and there is something to compare.

WHAT THIS MODULE DOES NOT DO. It does not execute, orchestrate, cache,
translate, validate a commit, or import a single KSOP module. Every target
below is a DOTTED PATH resolved at call time (see resolve()), which keeps
this table importable by anything -- a schema test, a frontend contract
dump, the cascade -- without dragging rasterio, shapely and the whole
pipeline in behind it, and which is what lets a test patch the target on its
own module and have the orchestrator pick the patch up.
"""

import importlib
from dataclasses import dataclass
from typing import Any, Optional

from design_document import STEP_ORDER

# --- where a consumed value comes from -------------------------------
#
# The vocabulary of `Consumed.source`. Two values, and the split is the
# proposal's section 3.2 worked example in one field: an interactive step's
# inputs are either DERIVED (recomputable from the boundary, so they live in
# the session cache) or DECIDED (a user's commit, so they live in the Design
# Document and reach the entry point through the inbound rehydrator).

# Read off the SessionContext -- the terrain warm-up's products and
# ParcelData's own layers. Rebuilt, not reloaded, if the cache was evicted;
# session_manager.get_session_context() makes that transparent.
SOURCE_CACHE = "cache"

# Read off a COMMITTED upstream step in the Design Document and rehydrated
# into its internal shape (proposal section 2.4). No entry below uses it
# yet -- landform is the first step, so it has no upstream commits -- but the
# resolver behind it is IMPLEMENTED (step_orchestrator._resolve_from_
# committed) rather than pending: a consumes edge to a committed step is what
# the cascade invalidates on, and the water entry is one row in this table
# plus one payload builder, not a resolver to write.
SOURCE_COMMITTED = "committed"

VALID_SOURCES = (SOURCE_CACHE, SOURCE_COMMITTED)


class RegistryError(Exception):
    """A malformed registry entry, or a request for a step that has none."""


def resolve(dotted_path: str) -> Any:
    """
    "module.attribute" -> the attribute, imported at CALL time.

    Late resolution is not laziness for its own sake. It keeps this table
    free of pipeline imports (see the module docstring), and it means a test
    that patches production_area_ceiling.identify_optimized_production_areas
    is patching the thing the orchestrator will actually call -- a target
    bound at import time would have captured the original function and every
    call-count assertion in test_step_orchestrator.py would be measuring the
    wrong object.
    """
    module_name, _, attribute = dotted_path.rpartition(".")
    if not module_name:
        raise RegistryError(
            f"'{dotted_path}' is not a dotted path; expected 'module.attribute'"
        )
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attribute)
    except AttributeError:
        raise RegistryError(
            f"'{dotted_path}' does not resolve: {module_name} has no '{attribute}'"
        ) from None


@dataclass(frozen=True)
class Consumed:
    """
    ONE input a step's generate needs, and the whole of what the orchestrator
    knows about it.

    name        the context field's name. Also the EDGE LABEL -- what the
                cascade names when it says why a step was invalidated.
    source      SOURCE_CACHE or SOURCE_COMMITTED.
    cache_path  source=cache: the dotted attribute path on the SessionContext
                ("dem", "parcel_data.canopy_height"). Attribute path rather
                than a plain name because ParcelData's layers hang off the
                context rather than sitting on it, and flattening them onto
                SessionContext to make this field a bare string would be
                changing the cache's shape to suit the registry's.
    from_step   source=committed: the upstream step whose commit supplies it.
    rehydrate   source=committed: the dotted path of the inbound translator
                (wire_translation.rehydrate_*) that turns that commit's
                GeoJSON back into the internal shape the override expects.
    empty_commit  source=committed: the dotted path of the SENTINEL VALUE
                this consumer must receive when the upstream step was
                committed with ZERO features, or None when the rehydrator's
                own empty answer already says it. See EMPTY IS AN ANSWER
                below -- this field is the whole of how a "committed
                nothing" decision survives the trip to a consumer.
    combine     the dotted path of a function from the RESOLVED SOURCE VALUE
                to the shape this override actually takes, or None when the
                source value already is that shape. See ONE SOURCE, ANOTHER
                SHAPE below.
    forward_as  the entry point's PARAMETER NAME for this value, or None.

    EMPTY IS AN ANSWER, AND MOST OVERRIDES CANNOT SAY IT. Every KSOP entry
    point in this pipeline treats a None override as "not supplied -- compute
    it yourself", which is exactly right for an absent value and exactly
    wrong for a deliberate empty commit. A user who commits the water step
    with nothing selected has DECIDED there is no water zone; forwarding that
    as None makes five downstream consumers each re-run the whole water
    suitability pipeline and hand back a zone the user rejected. That is not
    hypothetical -- it was measured at five water-suitability runs, and it is
    the reason water_suitability.NO_WATER_ZONE exists.

    So a consumer whose override parameter has a sentinel for "there is none
    of this" declares it here:

        empty_commit="water_suitability.NO_WATER_ZONE"

    and the resolver forwards THAT, never None, when the upstream commit is
    empty. A consumer whose rehydrated shape is a LIST needs no sentinel:
    [] already means "checked, nothing there" to every consumer of
    production_areas= (wire_translation.rehydrate_production_zones' own
    contract says so), and None is the value it must never be. Declaring
    empty_commit=None is therefore a claim -- "this rehydrator's empty
    answer is already explicit" -- not a gap, and the resolver enforces it
    by never substituting None of its own.

    ONE SOURCE, ANOTHER SHAPE -- `combine`, AND THE SECOND ENTRY IS WHAT
    EARNED IT. Landform needed none: what the cache holds and what the
    rehydrator returns are already exactly what identify_optimized_
    production_areas() takes, so the edge was an identity and the table did
    not have to say so. The water entry has two edges that are not, for two
    different reasons, and both are declarations about SHAPE rather than
    about computation:

      MANY COMMITTED FEATURES, ONE OVERRIDE VALUE. The water step commits a
      SET of selected survey zones, and every `selected_water_zone=`
      consumer takes ONE zone. The rehydrator returns the list (it must --
      the commit gate checks the features one at a time and the document
      records all of them), and `combine` names the reduction to the single
      unioned value the override wants: wire_translation.water_zone_union.

      ONE CACHE FIELD, A COMPOSITE OVERRIDE. `soil_inputs` is three
      ParcelData layers assembled into one dict, and only when ALL THREE are
      present -- a rule that is the water scorer's own all-or-nothing soil
      posture, not the cache's. `cache_path` names ONE attribute path by
      design (a composite path would be a small expression language in a
      table); `combine` names the function that assembles the override from
      the object that path resolves to.

    WHAT `combine` MAY NOT BE is a place to compute. It is applied to a
    value the resolvers already produced and its result is handed straight
    to the entry point, so a target that fetches, rasterises, or decides
    anything would be moving a step's work into the table's edges, where the
    cascade cannot see it and no test of the step's generate would cover it.
    Both targets above are a union and a dict literal.

    forward_as=None IS A REAL CASE, not a placeholder. A value can be a
    genuine input to the computation -- and therefore a genuine invalidation
    edge -- while the entry point derives it internally and exposes no
    override for it. boundary_polygon_utm is exactly that for landform:
    identify_optimized_production_areas() rebuilds it from boundary_
    coordinates and the DEM's CRS and takes no override. Declaring it with
    forward_as=None records the dependency truthfully instead of either
    lying about the call or dropping an edge the cascade needs. The
    orchestrator still assembles it and still hands it to the payload
    builder; it just does not pass it to the entry point.
    """

    name: str
    source: str
    forward_as: Optional[str] = None
    cache_path: Optional[str] = None
    from_step: Optional[str] = None
    rehydrate: Optional[str] = None
    empty_commit: Optional[str] = None
    combine: Optional[str] = None
    why: str = ""


@dataclass(frozen=True)
class LayerFailure:
    """
    One exception class this step's generate can raise, and the layer
    identity it reports as.

    DECLARED PER STEP because the answer is per step: a canopy failure is a
    landform failure and a hydrology-service failure will be the water
    step's, and neither orchestrator nor job runner should be the place that
    knows which. `layer`/`label` are the SAME two-field split
    exclusion_zones._wire_layers() and production_zone_payload.
    LayerFetchError use -- stable type to branch on, prose to print -- and
    the landform entry takes its pairs from production_zone_payload's own
    constants rather than restating them, so the session path and
    /api/production-zones name a failure identically.
    """

    exception: str  # dotted path
    layer: Optional[str] = None
    label: Optional[str] = None
    # The exception already carries its own .layer/.label (production_zone_
    # payload.LayerFetchError does, and it is the shape /api/production-zones
    # already puts on the wire). The orchestrator reads them off the INSTANCE,
    # so one raise site can name any layer without a row per layer here.
    self_describing: bool = False


# ======================================================================
# THE ONE SPATIAL HARD GATE, AND IT IS NOT A PER-STEP FIELD
# ======================================================================
#
# THE PARCEL BOUNDARY. For every step, now and later. A commit whose
# geometry leaves the parcel is rejected, naming the features that leave it;
# nothing else about where a feature sits can reject a commit.
#
# THIS REPLACES A PER-STEP `must_lie_within` FIELD, WHICH HELD
# "eligible_union". That was written from the architecture proposal's
# section 2.5 -- the server re-validates a commit against the same
# eligibility masks the client draws against, client advisory, server
# authoritative -- and that posture is REJECTED here in favour of the
# shipped frontend's, which is the settled contract:
#
#   zoneGeometry.js clampToBoundary(): "THE BOUNDARY IS THE ONLY HARD GATE.
#   Not the eligible union -- clamping to eligible ground would make the
#   caution system unreachable, because a user could never draw across
#   hydric soil to be warned about it. The rule is that gates encoding
#   physical impossibility apply and gates rejecting weak candidates do not:
#   off-parcel is not their land, while canopy, hydric, slope, roads and
#   setback are all conditions of ground they own and may commit to
#   knowingly."
#
# The exclusion gates are ADVISORY BY NATURE. A hydric rating is an
# inference off a survey polygon at survey scale; the person standing on the
# ground can see whether it is wet. ProductionZonePanel.jsx makes the same
# argument about its own 80% ceiling -- "having handed that judgment to the
# user ... taking it back at the gate would be incoherent". A server that
# hands the user five advisory gates and then refuses the commit they make
# in light of them is not being authoritative, it is being incoherent. So
# the server stays authoritative about what it can KNOW -- containment,
# geometric validity -- and advisory about what the user knows better, and
# it RECORDS every advisory crossing rather than rejecting it (see
# commit_validation.exclusion_crossings()).
#
# WHY CONTAINMENT IS LOAD-BEARING and not merely tidy: rehydration does NOT
# clip. wire_translation.rehydrate_production_zone()'s "NOT CLIPPED TO THE
# PARCEL, DELIBERATELY" note establishes why -- re-clipping would perturb an
# unedited zone's round-trip identity, and would silently REPAIR an
# off-parcel commit instead of reporting it. Which means this gate is the
# only thing between off-parcel geometry and every downstream consumer. Drop
# it and nothing catches it at all.
#
# A CONSTANT, NOT A FIELD, and that is the honest shape. A per-step field
# that can only ever hold one value is a false generalisation: it invites
# the next reader to believe some step somewhere validates against something
# else, and invites the next author to put a second value in it. If a step
# ever genuinely needs a different containment geometry, it earns a field
# then -- with a second real value to justify it.
COMMIT_MUST_LIE_WITHIN = "parcel_boundary"


@dataclass(frozen=True)
class CommitContract:
    """
    What a valid commit to this step looks like -- DECLARED HERE, ENFORCED
    IN commit_validation.py. The declaration belongs beside the consumes
    edges it constrains, because "what may be committed" and "what the next
    step then consumes" are one statement made twice if they are written in
    two places.

    THE HARD GATES, WHICH REJECT A COMMIT AND NAME THE OFFENDING FEATURES:

      * Boundary containment. See COMMIT_MUST_LIE_WITHIN above. The only
        spatial hard gate there is.
      * Geometric validity. Self-intersecting rings, degenerate or zero-area
        geometry, non-polygonal input, a zone covering no DEM cell centre.
        wire_translation's rehydrator already detects every one of these and
        raises InboundGeometryError naming the defect; the commit path
        surfaces that as a per-feature rejection rather than a 500.
      * The shape declarations below: layer, geometry_types, the feature
        count bounds, provenance.

    RECORDED, NOT REJECTED: exclusion crossings. A committed zone that
    overlaps the hydric, canopy, slope, roads or setback mask is VALID, and
    the crossing is written into the document alongside the feature.

    layers           the feature_schema layer name(s) a committed feature
                     may carry. A TUPLE, and the water entry is what earned
                     the plural: landform commits one layer
                     ("production_area_candidate") and this was a bare
                     string, but a water survey zone's TYPE is carried BY
                     ITS LAYER -- survey_zone_embankment and
                     survey_zone_excavated -- and a selection spans both
                     types freely, which is the product decision this step
                     exists to serve. Collapsing the two onto one layer to
                     fit a scalar field would delete the distinction the
                     frontend styles on; a per-step "and also this other
                     layer" escape hatch would be the same tuple with a
                     worse name. Same shape and same reasoning as
                     geometry_types below.
    geometry_types   permitted GeoJSON geometry types.
    min_features     the floor. ZERO for every step, and load-bearing: a
                     committed-empty step is a real decision ("nothing goes
                     here"), never a not_started one -- design_document.py's
                     own governing distinction.
    max_features     ceiling, or None for unbounded.
    rehydrate        the inbound translator a commit passes through on its
                     way to the internal shape (proposal section 2.4). It is
                     also, and not incidentally, the VALIDITY GATE: a
                     geometry that cannot be rehydrated cannot be committed.
    internal_id_parameter
                     the rehydrator's per-feature INTERNAL id keyword
                     ("zone_ids" for production zones), or None when the
                     rehydrator derives every id from the wire id alone.
                     A USER-DRAWN feature has no pipeline id to parse -- the
                     rehydrator says so and refuses to invent one, because an
                     invented id can collide with a generated zone's in the
                     same commit and silently merge their accounting. So the
                     COMMIT PATH allocates it, deterministically from the
                     committed collection, and hands it over under this name.
    requires_provenance  whether every committed feature must carry a
                     design_document.PROVENANCE_VALUES classification.
    """

    layers: tuple
    geometry_types: tuple
    min_features: int
    max_features: Optional[int]
    rehydrate: Optional[str]
    internal_id_parameter: Optional[str] = None
    requires_provenance: bool = True


@dataclass(frozen=True)
class PostCommitHook:
    """
    Something that must RE-RUN after a commit to this step lands, declared as
    data beside the step it belongs to.

    DECLARED, NOT BRANCHED. The keypoint relationship layer depends on
    committed production areas and on the selected water zone, so it must
    re-run after the landform commit AND after the water commit -- and the
    commit path must not be the place that knows that. An
    `if step_id == "landform"` in step_orchestrator.py would be the same
    failure the registry exists to prevent, one step's name compiled into the
    generic path.

    `target` is a dotted path resolved at call time like every other target
    in this table, called as:

        hook(context, document)

    -- the live SessionContext and the document AS JUST WRITTEN. A hook
    mutates the CONTEXT (tier 2, derived, disposable); it must not write to
    the document, which the commit path has already persisted. Anything a
    hook computes is therefore regenerable, which is what makes it safe to
    re-run and safe to lose.

    IDEMPOTENT BY REQUIREMENT. A hook may run after every commit to its step
    and after a re-commit; _attach_keypoint_feature_relationships() overwrites
    the key it sets rather than appending to it, and any hook added here must
    do the same.
    """

    target: str
    why: str = ""


@dataclass(frozen=True)
class StepDefinition:
    """One row of the registry. See the module docstring for the five fields."""

    step_id: str
    consumes: tuple
    generate: str  # dotted path
    produces: tuple
    commit_contract: CommitContract
    user_inputs: tuple = ()
    # PostCommitHooks, run in declaration order after a commit to this step
    # is persisted. See PostCommitHook for why this is data and not a branch.
    post_commit: tuple = ()
    # The key on this step's WIRE PAYLOAD holding its proposals as a
    # FeatureCollection.
    #
    # DECLARED BECAUSE THE REOPEN RESTORE READS IT. Restoring a reopened
    # step's editable state means re-running its generate and matching the
    # committed feature ids against the proposals that come back
    # (step_orchestrator.restore_step_state), and "which key holds the
    # proposals" is the one thing that walk cannot derive. Left undeclared it
    # would be a `payload["suggested_zones"]` in the generic path -- one
    # step's vocabulary compiled into code that is supposed to serve six.
    # The water entry will name its own key here and change nothing else.
    proposal_collection: str = ""
    # The step's outbound translation: internal generate result -> the wire
    # payload. A dotted path like every other target here, so this table
    # stays import-free and a test can patch the builder.
    payload: str = ""
    # Exceptions this step's generate raises that name a layer. Order is
    # significant -- first match wins -- so a subclass can be listed above
    # its base.
    failure_layers: tuple = ()
    # What a failure that matches no LayerFailure above reports. Mirrors
    # /api/production-zones' own generic branch, which sends an error string
    # and NO failed_layer: the frontend renders "The data sources did not
    # respond." for a null layer, which is the honest thing to say when the
    # failure was not one layer going down.
    generic_error: str = "This step could not be generated."

    def resolve_generate(self):
        return resolve(self.generate)

    def resolve_payload(self):
        return resolve(self.payload)

    def consumed_names(self) -> tuple:
        return tuple(consumed.name for consumed in self.consumes)

    def upstream_steps(self) -> tuple:
        """
        The committed steps this one reads, in STEP_ORDER. The cascade's
        edges, read off `consumes` rather than restated -- see the module
        docstring. Empty for landform: it is the first step and consumes
        only derived values.
        """
        steps = {
            consumed.from_step
            for consumed in self.consumes
            if consumed.source == SOURCE_COMMITTED and consumed.from_step
        }
        return tuple(step for step in STEP_ORDER if step in steps)


# ======================================================================
# THE REGISTRY
# ======================================================================

LANDFORM = StepDefinition(
    step_id="landform",
    consumes=(
        Consumed(
            name="boundary_coordinates",
            source=SOURCE_CACHE,
            cache_path="boundary",
            forward_as="boundary_coordinates",
            why=(
                "The parcel ring the whole step is computed over. Read off "
                "the context rather than the document so a rebuilt context "
                "and a warm one supply the identical value -- the context "
                "took it from the document at build time."
            ),
        ),
        Consumed(
            name="dem",
            source=SOURCE_CACHE,
            cache_path="dem",
            forward_as="dem",
            why=(
                "ParcelData's already-fetched elevation grid. Omitted, the "
                "entry point calls get_dem_for_boundary() itself -- a 3DEP "
                "raster fetch, on every regenerate."
            ),
        ),
        Consumed(
            name="boundary_polygon_utm",
            source=SOURCE_CACHE,
            cache_path="boundary_polygon_utm",
            forward_as=None,
            why=(
                "A real input with no override slot -- see Consumed's "
                "forward_as note. identify_optimized_production_areas() "
                "rebuilds this polygon from boundary_coordinates against "
                "dem['crs'], which is the same derivation ParcelData already "
                "performed, so the value is identical and the redundancy is "
                "arithmetic rather than a fetch. Declared because the "
                "exclusion masks below were computed against THIS polygon: "
                "it is a real edge even though it cannot be forwarded."
            ),
        ),
        Consumed(
            name="canopy_height",
            source=SOURCE_CACHE,
            cache_path="parcel_data.canopy_height",
            forward_as="canopy_height",
            why=(
                "ParcelData's already-fetched HAG layer. Forwarded for "
                "exactly the reason build_pipeline_context() forwards it "
                "even though exclusion_result makes production's own canopy "
                "fetch unreachable: the self-fetch path stays live for every "
                "caller that supplies no exclusion result, and dropping the "
                "override here would silently arm a redundant Planetary "
                "Computer fetch the day that pass-through changes."
            ),
        ),
        Consumed(
            name="exclusion_zones",
            source=SOURCE_CACHE,
            cache_path="exclusion_zones",
            forward_as="exclusion_result",
            why=(
                "LOAD-BEARING, and the reason this branch has a zero-SDA "
                "test. The terrain warm-up already ran identify_exclusion_"
                "zones() for this session (session_cache.run_terrain_warm_"
                "up()); forwarding its result is what makes generate "
                "network-free. WITHOUT it, identify_optimized_production_"
                "areas() takes its no-exclusion_result path, reaches "
                "production_area._fetch_disqualifying_soil_union(), and "
                "issues TWO SDA queries -- per generate, and generate is "
                "repeatable by contract. It also carries the canopy and road "
                "gate masks, so the same forward closes those two fetches. "
                "Identical to what build_pipeline_context() passes."
            ),
        ),
    ),
    generate="production_area_ceiling.identify_optimized_production_areas",
    payload="step_orchestrator.build_landform_payload",
    # production_zone_payload.assemble_production_zone_payload()'s own key --
    # the GeoJSON half of its two representations, which is the one carrying
    # feature ids. `zones` is the same zones as tabular rows for the panel.
    proposal_collection="suggested_zones",
    produces=(
        # PipelineContext's own field names, not new ones: the interactive
        # path accretes the SAME context the batch path does, one step at a
        # time (proposal section 2.3 -- "one mental model, two drivers").
        "production_areas",
        "parcel_acres",
    ),
    commit_contract=CommitContract(
        # wire_translation.LAYER_PRODUCTION_AREA. Spelled out rather than
        # imported for the reason in the module docstring -- this table
        # imports nothing from the pipeline -- and asserted equal to the
        # constant in test_step_registry.py so the two cannot drift.
        layers=("production_area_candidate",),
        # A drawn zone with a hole, or a zone the disc opening severed into
        # two lobes, are both real and both arrive as MultiPolygon.
        geometry_types=("Polygon", "MultiPolygon"),
        # Zero is a decision: "no production ground on this parcel", which
        # water and roads downstream must receive as an answer rather than
        # as an absence. See CommitContract.min_features.
        min_features=0,
        max_features=None,
        rehydrate="wire_translation.rehydrate_production_zones",
        # rehydrate_production_zones(collection, dem, zone_ids=...). See
        # CommitContract.internal_id_parameter: a drawn zone carries no
        # "production-area-<n>" id, so the commit path allocates one above
        # every id the same commit already uses.
        internal_id_parameter="zone_ids",
        requires_provenance=True,
    ),
    # NONE. The landform step runs on the traced boundary alone -- the same
    # reason /api/production-zones takes no access_point: nothing here routes
    # a road, so asking for one would be demanding a decision the user has
    # not been offered yet. (The roads step's entry will declare
    # access_point here.)
    user_inputs=(),
    post_commit=(
        PostCommitHook(
            target="step_orchestrator.attach_keypoint_relationships",
            why=(
                "The keypoint layer's feature_relationships are DERIVED FROM "
                "COMMITS -- distance and elevation differential from each "
                "keypoint to the nearest committed production area and to the "
                "selected water zone. Keypoints are not interactive and never "
                "appear in the Design Document (they are a read-only context "
                "layer, recomputed with the terrain warm-up), so the only way "
                "their relationship data can reflect a landform commit is for "
                "this to re-run after one. It re-runs again after the water "
                "commit, at which point the water half stops being "
                "'no_feature' -- which is exactly why it is DECLARED on the "
                "steps it depends on rather than branched on in the commit "
                "path."
            ),
        ),
    ),
    failure_layers=(
        # A LayerFetchError names its own layer, which is how /api/
        # production-zones reports one. Listed FIRST so it is matched before
        # any class-to-layer row below could shadow it.
        LayerFailure(
            exception="production_zone_payload.LayerFetchError",
            self_describing=True,
        ),
        # Canopy is MANDATORY on this path (exclusion_zones.py's GRACEFUL
        # DEGRADATION note): coverage too sparse to trust raises rather than
        # degrading, because "no trees here" and "we could not look" are
        # different claims about someone's land. The (layer, label) pair is
        # production_zone_payload.LAYER_CANOPY -- asserted equal to that
        # constant in test_step_registry.py rather than trusted to stay in
        # step, since the frontend's upstream-failure state branches on the
        # type and prints the label.
        LayerFailure(
            exception="canopy_height_data.CanopyCoverageIncompleteError",
            layer="canopy",
            label="tree canopy height",
        ),
    ),
    generic_error="Production zones could not be generated.",
)


WATER = StepDefinition(
    step_id="water",
    consumes=(
        # SEVEN EDGES: six off the cache, one off landform's commit. The
        # first four below are landform's own, at the same values; the next
        # two (roads, soil) are this step's alone; the last is the committed
        # one. Every override identify_water_survey_areas() does not get, it
        # FETCHES -- so the six cache edges are not conveniences, they are
        # what keeps a repeatable generate off the network.
        #
        # The four shared ones, first. Unlike landform, this step's entry
        # point exposes a real boundary_polygon_utm= override, so that edge
        # forwards rather than recording a dependency it cannot pass (see
        # Consumed.forward_as).
        Consumed(
            name="boundary_coordinates",
            source=SOURCE_CACHE,
            cache_path="boundary",
            forward_as="boundary_coordinates",
            why=(
                "The parcel ring the suitability surfaces are computed over, "
                "read off the context for landform's reason: a rebuilt "
                "context and a warm one supply the identical value."
            ),
        ),
        Consumed(
            name="dem",
            source=SOURCE_CACHE,
            cache_path="dem",
            forward_as="dem",
            why=(
                "ParcelData's already-fetched elevation grid. Omitted, the "
                "entry point calls get_dem_for_boundary() itself -- a 3DEP "
                "raster fetch, on every regenerate."
            ),
        ),
        Consumed(
            name="boundary_polygon_utm",
            source=SOURCE_CACHE,
            cache_path="boundary_polygon_utm",
            forward_as="boundary_polygon_utm",
            why=(
                "FORWARDED HERE, unlike landform's identically-named edge: "
                "identify_water_survey_areas() takes this override, and "
                "rebuilding it from boundary_coordinates against dem['crs'] "
                "would re-derive a polygon ParcelData already holds. It is "
                "also the polygon the gate mask, the parcel-relative TWI "
                "population and every envelope clip are measured against, so "
                "supplying the cache's own copy is what keeps this step's "
                "geometry identical to the exclusion masks' next door."
            ),
        ),
        Consumed(
            name="canopy_height",
            source=SOURCE_CACHE,
            cache_path="parcel_data.canopy_height",
            forward_as="canopy_height",
            why=(
                "ParcelData's already-fetched HAG layer. LOAD-BEARING here in "
                "a way it is not for landform: this step's canopy posture is "
                "FETCH-OR-RAISE (get_required_tree_root_zone_mask_utm at the "
                "water canopy buffer), so without the override every "
                "generate issues a Planetary Computer fetch and a coverage "
                "gap becomes a failed generate rather than a measured "
                "overlap."
            ),
        ),
        Consumed(
            name="existing_roads",
            source=SOURCE_CACHE,
            cache_path="existing_roads",
            forward_as="road_exclusion_union_utm",
            why=(
                "The road exclusion union the terrain warm-up already built "
                "from ParcelData's own rows and handed to the exclusion gate "
                "-- exactly what build_pipeline_context() forwards, at the "
                "same buffer. Closes this step's own road fetch. A REAL None "
                "is meaningful and is forwarded as itself: the entry point "
                "distinguishes 'not supplied' (its own sentinel default, "
                "which triggers the fetch) from None ('checked, genuinely no "
                "mapped road'), and the warm-up's None is the second."
            ),
        ),
        Consumed(
            name="soil_inputs",
            source=SOURCE_CACHE,
            cache_path="parcel_data",
            combine="water_survey_areas.soil_inputs_for_parcel_data",
            forward_as="soil_inputs",
            why=(
                "The soil trio -- ksat rows, component rows carrying hydgrp, "
                "clipped map-unit geometry -- assembled into the one override "
                "the scorer takes, ALL THREE OR NONE. Fetched once behind "
                "ParcelData's hard-fail contract, so forwarding it is what "
                "keeps this generate network-free; without it the entry point "
                "runs its own standalone whole-boundary soil fetch on every "
                "regenerate. The all-or-nothing rule is the water scorer's "
                "own and lives with it (see the combine target), not in this "
                "table and not in the cache."
            ),
        ),
        Consumed(
            name="production_areas",
            source=SOURCE_COMMITTED,
            from_step="landform",
            rehydrate="wire_translation.rehydrate_production_zones",
            forward_as="production_areas",
            why=(
                "THE FIRST COMMITTED EDGE IN THIS TABLE, and the reason the "
                "water step cannot be generated before landform is committed. "
                "The production ground a pond site is judged against -- "
                "production_overlap_pct, the gravity relationships, "
                "served_production_area_ids -- must be the ground the USER "
                "chose, not the optimiser's own answer. The entry point's "
                "None path re-runs identify_optimized_production_areas() and "
                "sites water against zones the user may have rejected, which "
                "is precisely the plausible-wrong-answer failure "
                "UpstreamNotCommittedError exists to refuse. "
                "build_pipeline_context() forwards scored_patches into this "
                "same parameter; the rehydrated commit is the same shape, "
                "with each selected proposal keeping its own pipeline id so "
                "served_production_area_ids still names something."
            ),
        ),
    ),
    generate="water_survey_areas.identify_water_survey_areas",
    payload="step_orchestrator.build_water_payload",
    # ONE ENTRY POINT, ONE ZONE LIST. Both survey types come back from the
    # single call with `survey_type` on each zone; SURVEY_TYPES drives the
    # per-type logic inside the module. This is deliberately NOT two generate
    # targets: the two surfaces share the gate mask, the soil scorer and the
    # derived screens, and cross_type_overlaps is an agreement report BETWEEN
    # them that only exists because one call sees both.
    proposal_collection="survey_zones",
    produces=(
        # PipelineContext's own field names, as landform's are. water_zones is
        # the flagged-not-filtered zone+member FeatureCollection's features;
        # selected_water_zone is the one value downstream overrides take --
        # here the UNION of the user's selection rather than the batch path's
        # pooled rank-1 pick.
        "water_zones",
        "selected_water_zone",
    ),
    commit_contract=CommitContract(
        # BOTH ZONE LAYERS. wire_translation.LAYER_SURVEY_ZONES, spelled out
        # rather than imported (this table imports nothing from the pipeline)
        # and asserted equal to that constant in test_step_registry.py. The
        # member and dropped layers are absent on purpose: a member footprint
        # is a sub-feature of a zone and a dropped zone is below the acreage
        # floor, so neither is selectable and a commit carrying one is
        # rejected by name.
        layers=("survey_zone_embankment", "survey_zone_excavated"),
        # A zone envelope is a convex hull clipped to the parcel, so a
        # concave-boundary parcel can cut one into several pieces.
        geometry_types=("Polygon", "MultiPolygon"),
        # ZERO IS A DECISION -- "no water system on this parcel" -- and this
        # is the step where that stops being theoretical. It reaches the five
        # downstream consumers as the empty_commit sentinel below, never as
        # None. See CommitContract.min_features and Consumed.empty_commit.
        min_features=0,
        # NO CEILING. Multi-select is the product decision: a user may select
        # any number of zones across both types, and downstream consumes the
        # union of them. A cap here would be this table deciding how many
        # ponds a farm may have.
        max_features=None,
        rehydrate="wire_translation.rehydrate_water_survey_zones",
        # NONE, and the contrast with landform is the whole story of this
        # step. internal_id_parameter exists because a DRAWN zone has no
        # pipeline id and the commit path must allocate one. Water is
        # SELECT-ONLY: every committable feature is one this pipeline
        # generated and handed to the client, carrying its own
        # "water-survey-zone-<n>". So there is nothing to allocate, and the
        # rehydrator refuses a feature whose id does not parse rather than
        # inventing one -- an invented id would be a survey recommendation
        # for ground no suitability surface ever nominated.
        internal_id_parameter=None,
        # STILL REQUIRED, even though every zone is generated. The
        # classification is the user's own statement about what a feature is,
        # and a commit that omits it is a client that has stopped saying --
        # which is exactly when a drawn shape could start arriving unnoticed
        # if this step ever gains an editor.
        requires_provenance=True,
    ),
    # NONE. Selecting a survey zone collects no extra parameter: the
    # suitability surfaces are computed over the whole parcel and the user's
    # input IS the selection.
    user_inputs=(),
    # NO POST-COMMIT HOOK, AND THIS IS A REPORTED GAP RATHER THAN AN
    # OVERSIGHT. attach_keypoint_relationships (declared by landform) writes
    # each keypoint's distance and elevation differential to the selected
    # water zone, and it reads representative_elevation_m off that zone. The
    # value forwarded here is a UNION of the selected zones, which has no
    # single representative elevation -- the honest answer is per-keypoint,
    # against the NEAREST selected zone, and that is a change to
    # pipeline_context._attach_keypoint_feature_relationships()'s signature
    # and to what the BATCH pipeline means by the keypoint water
    # relationship. Declaring the hook here without that change would make it
    # re-run after a real water commit and write "no_feature" for a selection
    # the user actually made, turning a truthful answer into a false one. So
    # the hook stays on landform alone and the water half of every keypoint
    # relationship keeps reading "no_feature", exactly as it does today.
    post_commit=(),
    failure_layers=(
        # Canopy is MANDATORY on this path too, and for a stronger reason
        # than landform's: canopy_overlap_pct is one of the three sentinel
        # measurements every zone carries, and its None/0.0 split ("never
        # checked" vs "checked and genuinely none") is only meaningful
        # because the mask is fetch-or-RAISE. Degrading here would print "no
        # trees on this pond site" for ground nobody looked at. The pair is
        # production_zone_payload.LAYER_CANOPY, asserted equal to that
        # constant in test_step_registry.py.
        LayerFailure(
            exception="canopy_height_data.CanopyCoverageIncompleteError",
            layer="canopy",
            label="tree canopy height",
        ),
    ),
    generic_error="Water survey areas could not be generated.",
)


STEP_REGISTRY = {
    LANDFORM.step_id: LANDFORM,
    WATER.step_id: WATER,
}


# --- reading the registry --------------------------------------------


def registered_steps() -> tuple:
    """
    The steps that can be generated, in STEP_ORDER. Filters that constant --
    it does not restate the order. Shorter than STEP_ORDER until stage 3 is
    finished, and that difference is the honest report of what exists.
    """
    return tuple(step_id for step_id in STEP_ORDER if step_id in STEP_REGISTRY)


def get_step(step_id: str) -> StepDefinition:
    """
    One step's definition. Raises RegistryError -- naming what IS registered
    -- for a step that has no entry, including a real STEP_ORDER step whose
    entry is simply not written yet. Those two cases are told apart in the
    message, because "water is not a step" and "water is not implemented" are
    different things to be told.
    """
    definition = STEP_REGISTRY.get(step_id)
    if definition is not None:
        return definition
    if step_id in STEP_ORDER:
        raise RegistryError(
            f"step '{step_id}' has no registry entry yet; registered steps "
            f"are {registered_steps()}"
        )
    raise RegistryError(
        f"unknown step id '{step_id}'; the design document's steps are "
        f"{STEP_ORDER} and the registered ones are {registered_steps()}"
    )


def dependents_of(step_id: str) -> tuple:
    """
    Every REGISTERED step that consumes a commit of `step_id`, in STEP_ORDER
    -- the invalidation edges, read straight off the consumes declarations.

    B5b's cascade walks this transitively. It is deliberately NOT
    design_document.downstream_steps(): that function resets everything AFTER
    a step because the document cannot know what actually depends on what,
    which is the conservative answer. This one is the precise answer, and the
    two are allowed to differ -- a step later in KSOP order that consumes
    nothing from landform does not have to be invalidated by a landform
    re-commit. Empty today for the same reason upstream_steps() is: only one
    entry exists.
    """
    if step_id not in STEP_ORDER:
        raise RegistryError(f"unknown step id '{step_id}'")
    return tuple(
        other
        for other in registered_steps()
        if step_id in STEP_REGISTRY[other].upstream_steps()
    )


def transitive_dependents(step_id: str) -> tuple:
    """
    Every REGISTERED step reachable from `step_id` along consumes edges, at
    any depth, in STEP_ORDER. What a reopen or a re-commit of `step_id`
    invalidates.

    TRANSITIVE BECAUSE THE STALENESS IS. If roads consumes water's commit and
    water consumes landform's, then reopening landform makes the ROADS
    proposals stale too -- they were computed from a water answer that was
    itself computed from the landform commit now being re-edited. A one-hop
    walk would leave those proposals in the cache looking current.

    Excludes `step_id` itself. Cycles cannot occur -- validate_registry()
    rejects any consumes edge that is not strictly upstream in STEP_ORDER --
    so this terminates without a visited-set guard; it keeps one anyway,
    because a walk that would loop forever on a malformed table is a worse
    failure than one that returns a wrong answer loudly.
    """
    if step_id not in STEP_ORDER:
        raise RegistryError(f"unknown step id '{step_id}'")
    reached = set()
    frontier = [step_id]
    while frontier:
        current = frontier.pop()
        for dependent in dependents_of(current):
            if dependent not in reached:
                reached.add(dependent)
                frontier.append(dependent)
    return tuple(step for step in registered_steps() if step in reached)


def validate_registry() -> None:
    """
    Structural validation of every entry, in the same fail-loud posture
    design_document.validate_document() takes: raises RegistryError, never
    repairs, never defaults. Called by test_step_registry.py and cheap enough
    for a caller to run at startup.

    Checks the SHAPE, and resolves nothing -- import-free is the point (see
    the module docstring). test_step_registry.py resolves every target
    separately and asserts it is callable, which is the check that needs the
    pipeline in memory.
    """
    for step_id, definition in STEP_REGISTRY.items():
        where = f"registry entry '{step_id}'"
        if definition.step_id != step_id:
            raise RegistryError(
                f"{where} is keyed '{step_id}' but declares step_id "
                f"'{definition.step_id}'"
            )
        if step_id not in STEP_ORDER:
            raise RegistryError(
                f"{where} is not a design_document.STEP_ORDER step; the "
                f"registry may not invent steps the document cannot hold"
            )
        if not definition.generate:
            raise RegistryError(f"{where} declares no generate target")
        if not definition.payload:
            raise RegistryError(f"{where} declares no payload builder")
        if not definition.proposal_collection:
            raise RegistryError(
                f"{where} names no proposal_collection; the reopen restore "
                f"cannot find this step's proposals on its own payload"
            )
        if not definition.produces:
            raise RegistryError(
                f"{where} produces nothing; a step that contributes no "
                f"context field has nothing for a later step to consume"
            )
        if not isinstance(definition.commit_contract, CommitContract):
            raise RegistryError(f"{where} has no CommitContract")
        if definition.commit_contract.min_features < 0:
            raise RegistryError(f"{where} declares a negative min_features")

        seen = set()
        for consumed in definition.consumes:
            if consumed.name in seen:
                raise RegistryError(f"{where} consumes '{consumed.name}' twice")
            seen.add(consumed.name)
            if consumed.source not in VALID_SOURCES:
                raise RegistryError(
                    f"{where}: consumed '{consumed.name}' has source "
                    f"{consumed.source!r}; must be one of {VALID_SOURCES}"
                )
            if consumed.source == SOURCE_CACHE:
                if not consumed.cache_path:
                    raise RegistryError(
                        f"{where}: cache-sourced '{consumed.name}' declares "
                        f"no cache_path"
                    )
                if consumed.from_step or consumed.rehydrate or consumed.empty_commit:
                    raise RegistryError(
                        f"{where}: cache-sourced '{consumed.name}' declares "
                        f"from_step/rehydrate/empty_commit, which only a "
                        f"committed source uses"
                    )
            else:
                if not consumed.from_step:
                    raise RegistryError(
                        f"{where}: committed '{consumed.name}' declares no "
                        f"from_step"
                    )
                if consumed.from_step not in STEP_ORDER:
                    raise RegistryError(
                        f"{where}: consumed '{consumed.name}' names unknown "
                        f"upstream step '{consumed.from_step}'"
                    )
                # STRICTLY UPSTREAM. A step consuming a later step's commit
                # is a cycle in the cascade, and STEP_ORDER is what settles
                # which way the edges point.
                if STEP_ORDER.index(consumed.from_step) >= STEP_ORDER.index(step_id):
                    raise RegistryError(
                        f"{where}: consumed '{consumed.name}' comes from "
                        f"'{consumed.from_step}', which is not upstream of "
                        f"'{step_id}' in STEP_ORDER"
                    )
                if not consumed.rehydrate:
                    raise RegistryError(
                        f"{where}: committed '{consumed.name}' declares no "
                        f"rehydrate translator; a commit reaches an override "
                        f"only through the inbound boundary"
                    )

        for hook in definition.post_commit:
            if not isinstance(hook, PostCommitHook):
                raise RegistryError(
                    f"{where} declares a post_commit entry that is not a "
                    f"PostCommitHook: {hook!r}"
                )
            if not hook.target:
                raise RegistryError(f"{where} declares a post-commit hook with no target")

        contract = definition.commit_contract
        if not contract.layers:
            raise RegistryError(f"{where}'s commit contract names no layer")
        if isinstance(contract.layers, str):
            raise RegistryError(
                f"{where}'s commit contract declares layers as a bare string "
                f"{contract.layers!r}; it is a TUPLE of layer names, and a "
                f"string would be iterated one character at a time"
            )
        if not contract.geometry_types:
            raise RegistryError(
                f"{where}'s commit contract permits no geometry type, so no "
                f"commit to it could ever be valid"
            )
        if not contract.rehydrate:
            raise RegistryError(
                f"{where}'s commit contract declares no rehydrator; a commit "
                f"reaches an internal shape -- and is checked for geometric "
                f"validity -- only through the inbound boundary"
            )
        if contract.max_features is not None and contract.max_features < contract.min_features:
            raise RegistryError(
                f"{where} declares max_features {contract.max_features} below "
                f"min_features {contract.min_features}"
            )

        forwarded = [c.forward_as for c in definition.consumes if c.forward_as]
        if len(forwarded) != len(set(forwarded)):
            raise RegistryError(
                f"{where} forwards two consumed values into the same "
                f"parameter: {sorted(forwarded)}"
            )
        overlap = set(forwarded) & set(definition.user_inputs)
        if overlap:
            raise RegistryError(
                f"{where}: user input(s) {sorted(overlap)} collide with a "
                f"forwarded consumed value's parameter"
            )
