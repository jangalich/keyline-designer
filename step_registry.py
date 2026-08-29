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
# yet: landform is the first step, so it has no upstream commits, and the
# steps that do are B5b's. The vocabulary carries it now because a consumes
# edge to a committed step is what the cascade invalidates ON -- see
# step_orchestrator._CONSUMES_RESOLVERS for the one-line extension point.
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
    forward_as  the entry point's PARAMETER NAME for this value, or None.

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


@dataclass(frozen=True)
class CommitContract:
    """
    What a valid commit to this step looks like. DECLARED HERE, ENFORCED IN
    B5b -- this branch records the contract and enforces none of it, which is
    why every field is a description rather than a validator. Commit
    validation is server-authoritative (proposal section 2.5) and belongs
    with the commit path, but the DECLARATION belongs here beside the
    consumes edges it constrains, because "what may be committed" and "what
    the next step then consumes" are one statement made twice if they are
    written in two places.

    layer            the feature_schema layer name the committed
                     FeatureCollection's features must carry.
    geometry_types   permitted GeoJSON geometry types.
    min_features     the floor. ZERO for every step, and load-bearing: a
                     committed-empty step is a real decision ("nothing goes
                     here"), never a not_started one -- design_document.py's
                     own governing distinction.
    max_features     ceiling, or None for unbounded.
    must_lie_within  the name of the eligibility geometry a commit is
                     validated against, as it appears on the generate
                     result. B5b intersects against this and records the
                     crossing; nothing here does.
    rehydrate        the inbound translator a commit passes through on its
                     way to the internal shape (proposal section 2.4).
    requires_provenance  whether every committed feature must carry a
                     design_document.PROVENANCE_VALUES classification.
    """

    layer: str
    geometry_types: tuple
    min_features: int
    max_features: Optional[int]
    must_lie_within: Optional[str]
    rehydrate: Optional[str]
    requires_provenance: bool = True


@dataclass(frozen=True)
class StepDefinition:
    """One row of the registry. See the module docstring for the five fields."""

    step_id: str
    consumes: tuple
    generate: str  # dotted path
    produces: tuple
    commit_contract: CommitContract
    user_inputs: tuple = ()
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
        layer="production_area_candidate",
        # A drawn zone with a hole, or a zone the disc opening severed into
        # two lobes, are both real and both arrive as MultiPolygon.
        geometry_types=("Polygon", "MultiPolygon"),
        # Zero is a decision: "no production ground on this parcel", which
        # water and roads downstream must receive as an answer rather than
        # as an absence. See CommitContract.min_features.
        min_features=0,
        max_features=None,
        # exclusion_zones' eligible geometry -- the mask the frontend already
        # draws as its ineligible overlay and already intersects drawn
        # polygons against client-side. B5b re-validates against it
        # server-side (proposal section 2.5); the client's copy is advisory.
        must_lie_within="eligible_union",
        rehydrate="wire_translation.rehydrate_production_zones",
        requires_provenance=True,
    ),
    # NONE. The landform step runs on the traced boundary alone -- the same
    # reason /api/production-zones takes no access_point: nothing here routes
    # a road, so asking for one would be demanding a decision the user has
    # not been offered yet. (The roads step's entry will declare
    # access_point here.)
    user_inputs=(),
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


STEP_REGISTRY = {
    LANDFORM.step_id: LANDFORM,
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
                if consumed.from_step or consumed.rehydrate:
                    raise RegistryError(
                        f"{where}: cache-sourced '{consumed.name}' declares "
                        f"from_step/rehydrate, which only a committed source "
                        f"uses"
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
