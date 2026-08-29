"""
test_step_registry.py

The Step Registry's declared shape, run as:

    python test_step_registry.py

WHAT THIS FILE IS FOR. step_registry.py is a TABLE, and a table's failure
mode is a typo that nothing notices until the orchestrator reads it at
runtime. Everything asserted here is structural: the entry's shape, the
resolvability and callability of every dotted target, the agreement between
declared constants and the constants they mirror, and the step-order
invariants the cascade will depend on.

NO PIPELINE RUNS HERE. The registry is import-free by design; these
assertions resolve its targets (which does import the pipeline) but call
none of them. test_step_orchestrator.py is where the landform entry is
actually executed over real coordinates.

Sections:
  1. VALIDATION -- validate_registry() passes; every deliberate malformation
     of a copy of the landform entry is rejected.
  2. LANDFORM'S DECLARED SHAPE -- every field, field by field.
  3. TARGETS RESOLVE AND ARE CALLABLE -- generate, payload, rehydrate, and
     every declared failure exception.
  4. THE GENERATE TARGET'S SIGNATURE ACCEPTS WHAT IS FORWARDED -- every
     forward_as is a real parameter of the real function.
  5. CONSTANTS AGREE -- the layer name, the failure labels, and the feature
     id prefix, each against the module that owns it.
  6. STEP ORDER -- registered_steps() filters STEP_ORDER; the edge helpers
     agree with it.
"""

import dataclasses
import inspect

import step_registry
from design_document import STEP_ORDER

# --- 1. VALIDATION ----------------------------------------------------

step_registry.validate_registry()

assert step_registry.registered_steps() == ("landform",), (
    f"one entry is expected on this branch: {step_registry.registered_steps()}"
)
assert set(step_registry.STEP_REGISTRY) <= set(STEP_ORDER), (
    "the registry may not invent steps the design document cannot hold"
)

_LANDFORM = step_registry.get_step("landform")


def _rejects(broken, why, key=None):
    """A one-entry registry built from `broken` must fail validate_registry().

    `key` overrides the dict key, so the "keyed against a different step_id"
    case can actually be built -- keying by broken.step_id would make that
    entry self-consistent and the check vacuous."""
    original = step_registry.STEP_REGISTRY
    step_registry.STEP_REGISTRY = {key or broken.step_id: broken}
    try:
        step_registry.validate_registry()
    except step_registry.RegistryError:
        return
    finally:
        step_registry.STEP_REGISTRY = original
    raise AssertionError(f"validate_registry() accepted a malformed entry: {why}")


_C = step_registry.Consumed
_rejects(
    dataclasses.replace(_LANDFORM, generate=""), "no generate target"
)
_rejects(
    dataclasses.replace(_LANDFORM, payload=""), "no payload builder"
)
_rejects(
    dataclasses.replace(_LANDFORM, produces=()), "produces nothing"
)
_rejects(
    dataclasses.replace(_LANDFORM, step_id="water"),
    "keyed against a different step_id",
    key="landform",
)
_rejects(
    dataclasses.replace(_LANDFORM, step_id="orchards"),
    "a step the design document cannot hold",
)
_rejects(
    dataclasses.replace(
        _LANDFORM,
        consumes=_LANDFORM.consumes + (_C(name="dem", source="cache", cache_path="dem"),),
    ),
    "the same value consumed twice",
)
_rejects(
    dataclasses.replace(
        _LANDFORM,
        consumes=(_C(name="x", source="telepathy", cache_path="dem"),),
    ),
    "an unknown source",
)
_rejects(
    dataclasses.replace(_LANDFORM, consumes=(_C(name="x", source="cache"),)),
    "a cache source with no cache_path",
)
_rejects(
    dataclasses.replace(
        _LANDFORM,
        consumes=(
            _C(
                name="x",
                source="committed",
                from_step="water",
                rehydrate="wire_translation.rehydrate_production_zones",
            ),
        ),
    ),
    "landform consuming a commit from water, which is DOWNSTREAM of it",
)
_rejects(
    dataclasses.replace(
        _LANDFORM,
        consumes=(_C(name="x", source="committed", from_step="landform"),),
    ),
    "a committed source with no rehydrate translator",
)
_rejects(
    dataclasses.replace(
        _LANDFORM,
        consumes=(
            _C(name="a", source="cache", cache_path="dem", forward_as="dem"),
            _C(name="b", source="cache", cache_path="dem", forward_as="dem"),
        ),
    ),
    "two consumed values forwarded into one parameter",
)
_rejects(
    dataclasses.replace(_LANDFORM, user_inputs=("dem",)),
    "a user input colliding with a forwarded parameter",
)

print(
    "1. VALIDATION: validate_registry() passes on the real registry; 11 "
    "deliberate malformations of the landform entry are each rejected."
)


# --- 2. LANDFORM'S DECLARED SHAPE ------------------------------------

assert _LANDFORM.step_id == "landform"
assert _LANDFORM.generate == "production_area_ceiling.identify_optimized_production_areas", (
    f"the landform generate target is the ceiling optimizer's entry point, got "
    f"{_LANDFORM.generate!r}"
)
assert _LANDFORM.produces == ("production_areas", "parcel_acres"), (
    f"landform contributes PipelineContext's own two production fields, got "
    f"{_LANDFORM.produces}"
)
assert _LANDFORM.user_inputs == (), (
    "the landform step runs on the traced boundary alone -- no user inputs"
)

_consumed = {c.name: c for c in _LANDFORM.consumes}
assert set(_consumed) == {
    "boundary_coordinates",
    "dem",
    "boundary_polygon_utm",
    "canopy_height",
    "exclusion_zones",
}, f"landform's consumes set: {sorted(_consumed)}"

assert all(c.source == step_registry.SOURCE_CACHE for c in _LANDFORM.consumes), (
    "landform is the first step: it has no upstream commits, so every "
    "consumed value is cache-sourced"
)
assert _LANDFORM.upstream_steps() == (), (
    f"landform has no upstream committed steps: {_LANDFORM.upstream_steps()}"
)

# THE FORWARD THAT KEEPS GENERATE NETWORK-FREE. Asserted in the registry as
# well as in the orchestrator test, because the SDA-count assertion over
# there can only fail loudly if this declaration is what it is checking.
assert _consumed["exclusion_zones"].forward_as == "exclusion_result", (
    f"the warm-up's exclusion result MUST forward into exclusion_result=; got "
    f"{_consumed['exclusion_zones'].forward_as!r}. Without it identify_"
    f"optimized_production_areas() takes its self-fetch path and issues two "
    f"SDA queries per generate."
)
assert _consumed["exclusion_zones"].cache_path == "exclusion_zones"
assert _consumed["dem"].forward_as == "dem"
assert _consumed["canopy_height"].forward_as == "canopy_height"
assert _consumed["canopy_height"].cache_path == "parcel_data.canopy_height"
assert _consumed["boundary_coordinates"].forward_as == "boundary_coordinates"
assert _consumed["boundary_polygon_utm"].forward_as is None, (
    "identify_optimized_production_areas() derives boundary_polygon_utm "
    "itself and exposes no override; declaring a forward_as for it would be "
    "declaring a call that cannot be made"
)
assert all(c.why for c in _LANDFORM.consumes), (
    "every consumes edge carries its own reason -- these are the cascade's "
    "edges and a bare name does not say why cutting one invalidates a step"
)

_contract = _LANDFORM.commit_contract
assert _contract.min_features == 0, (
    "zero committed zones is a real decision ('no production ground here'), "
    "never a not_started step -- design_document.py's governing distinction"
)
assert _contract.max_features is None
assert _contract.geometry_types == ("Polygon", "MultiPolygon")
assert _contract.must_lie_within == "eligible_union"
assert _contract.rehydrate == "wire_translation.rehydrate_production_zones"
assert _contract.requires_provenance is True

print(
    f"2. LANDFORM SHAPE: consumes {len(_LANDFORM.consumes)} values "
    f"({', '.join(sorted(_consumed))}), forwards "
    f"{len([c for c in _LANDFORM.consumes if c.forward_as])} of them as "
    f"overrides, produces {_LANDFORM.produces}, declares a commit contract "
    f"({_contract.layer}, >={_contract.min_features} features, within "
    f"{_contract.must_lie_within}) and {len(_LANDFORM.user_inputs)} user inputs."
)


# --- 3. TARGETS RESOLVE AND ARE CALLABLE ------------------------------

_generate_target = _LANDFORM.resolve_generate()
assert callable(_generate_target), (
    f"the declared generate target must be callable: {_generate_target!r}"
)
_payload_target = _LANDFORM.resolve_payload()
assert callable(_payload_target)
assert callable(step_registry.resolve(_contract.rehydrate))

for _failure in _LANDFORM.failure_layers:
    _exception_class = step_registry.resolve(_failure.exception)
    assert isinstance(_exception_class, type) and issubclass(
        _exception_class, BaseException
    ), f"declared failure {_failure.exception} is not an exception class"

_resolve_errors = 0
for _bad in ("nosuchmodule.thing", "step_registry.no_such_attribute", "bare"):
    try:
        step_registry.resolve(_bad)
    except (step_registry.RegistryError, ImportError):
        _resolve_errors += 1
assert _resolve_errors == 3, "resolve() must fail loudly on an unresolvable path"

print(
    f"3. TARGETS: generate ({_generate_target.__module__}."
    f"{_generate_target.__name__}), payload ({_payload_target.__module__}."
    f"{_payload_target.__name__}), the inbound rehydrator, and "
    f"{len(_LANDFORM.failure_layers)} declared failure exception classes all "
    f"resolve; 3 unresolvable paths each raise."
)


# --- 4. THE FORWARDED PARAMETERS ARE REAL PARAMETERS ------------------
#
# The assertion that catches the typo class this table is most exposed to: a
# forward_as naming a parameter the entry point does not have. It would not
# fail at import, only at the first generate, as a TypeError from deep inside
# a call the reader has to reconstruct.

_signature = inspect.signature(_generate_target)
for _c in _LANDFORM.consumes:
    if _c.forward_as is None:
        continue
    assert _c.forward_as in _signature.parameters, (
        f"consumed '{_c.name}' forwards as '{_c.forward_as}', which is not a "
        f"parameter of {_LANDFORM.generate}: "
        f"{sorted(_signature.parameters)}"
    )

# The forwarded set matches what build_pipeline_context() forwards into this
# same function. The batch path and the session path are one computation
# reached two ways (proposal section 2.3), so a divergence here is the two
# drivers starting to disagree.
_BATCH_FORWARDS = {"boundary_coordinates", "dem", "canopy_height", "exclusion_result"}
_registry_forwards = {c.forward_as for c in _LANDFORM.consumes if c.forward_as}
assert _registry_forwards == _BATCH_FORWARDS, (
    f"the landform entry must forward exactly what build_pipeline_context() "
    f"forwards into identify_optimized_production_areas(): expected "
    f"{sorted(_BATCH_FORWARDS)}, got {sorted(_registry_forwards)}"
)

print(
    f"4. SIGNATURE: every forwarded parameter {sorted(_registry_forwards)} is "
    f"a real parameter of {_LANDFORM.generate}, and the set matches what "
    f"build_pipeline_context() forwards into it."
)


# --- 5. CONSTANTS AGREE WITH THE MODULES THAT OWN THEM -----------------

import production_zone_payload  # noqa: E402  -- after the resolve checks above
import wire_translation  # noqa: E402

assert _contract.layer == wire_translation.LAYER_PRODUCTION_AREA, (
    f"the commit contract's layer must be wire_translation's own constant: "
    f"{_contract.layer!r} vs {wire_translation.LAYER_PRODUCTION_AREA!r}"
)

_canopy = next(f for f in _LANDFORM.failure_layers if f.layer == "canopy")
assert (_canopy.layer, _canopy.label) == production_zone_payload.LAYER_CANOPY, (
    f"the canopy failure's (type, label) must be the SAME pair "
    f"/api/production-zones sends: {(_canopy.layer, _canopy.label)} vs "
    f"{production_zone_payload.LAYER_CANOPY}. The panel branches on the type "
    f"and prints the label."
)
assert _LANDFORM.generic_error == "Production zones could not be generated.", (
    "the unclassified-failure prose is the endpoint's own 500 body"
)
_self_describing = [f for f in _LANDFORM.failure_layers if f.self_describing]
assert len(_self_describing) == 1 and _self_describing[0].exception == (
    "production_zone_payload.LayerFetchError"
), "LayerFetchError names its own layer and must be the self-describing row"

print(
    f"5. CONSTANTS: commit contract layer == wire_translation."
    f"LAYER_PRODUCTION_AREA ({_contract.layer!r}); the canopy failure pair == "
    f"production_zone_payload.LAYER_CANOPY {production_zone_payload.LAYER_CANOPY}; "
    f"the generic error is the endpoint's own prose."
)


# --- 6. STEP ORDER ----------------------------------------------------

assert step_registry.registered_steps() == tuple(
    s for s in STEP_ORDER if s in step_registry.STEP_REGISTRY
), "registered_steps() must FILTER STEP_ORDER, never restate an order"

for _unregistered in ("water", "roads", "trees", "structures", "fencing"):
    try:
        step_registry.get_step(_unregistered)
    except step_registry.RegistryError as exc:
        assert "no registry entry yet" in str(exc), (
            f"a real STEP_ORDER step without an entry must say so: {exc}"
        )
    else:
        raise AssertionError(f"'{_unregistered}' should have no registry entry")

try:
    step_registry.get_step("orchards")
except step_registry.RegistryError as exc:
    assert "unknown step id" in str(exc)
else:
    raise AssertionError("a step outside STEP_ORDER must be reported as unknown")

assert step_registry.dependents_of("landform") == (), (
    "nothing registered consumes a landform commit yet -- water is B5b's"
)

print(
    f"6. STEP ORDER: registered_steps() filters design_document.STEP_ORDER "
    f"{STEP_ORDER} to {step_registry.registered_steps()}; the five "
    f"unregistered steps each report 'no registry entry yet' and a step "
    f"outside STEP_ORDER reports 'unknown step id'."
)


print("\nAll step_registry checks passed.")
