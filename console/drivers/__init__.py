"""The driver registry, and the resolver that walks a descriptor's bindings.

One driver per source **shape** (§2.7). Registering one adds a shape the whole
fleet can bind to; it never adds knowledge of any component.

The resolver is index-level orchestration, deliberately *not* inside the
registry adapter: §2.3 says an adapter reads exactly one source and nothing
else knows that source at all. The registry adapter reads the registry. Walking
what the registry declared and calling other sources is a different job, and
putting it in the adapter would make the registry adapter a reader of every
source in the fleet.
"""
from __future__ import annotations

from typing import Any

from ..model.descriptor import Binding, Descriptor
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass
from . import emitted_envelope, object_store
from .base import Cost, DriverResult

#: shape name → module. Adding a source SHAPE is adding a row here; adding a
#: source *instance* is a line in one component's descriptor and touches
#: nothing in this repository (§2.6).
DRIVERS: dict[str, Any] = {
    object_store.name: object_store,
    emitted_envelope.name: emitted_envelope,
}

KNOWN_DRIVERS = frozenset(DRIVERS)

__all__ = ["DRIVERS", "KNOWN_DRIVERS", "Cost", "DriverResult",
           "resolve_bindings"]


def resolve_bindings(
    descriptors: list[Descriptor],
    context: dict[str, Any] | None = None,
) -> list[AdapterResult]:
    """Read every binding every descriptor declared, one result per driver.

    Grouped **by driver**, not by component, so the index sees one claim source
    per shape — which is what makes `§5.9`'s per-source fetch record and
    `doctor`'s per-binding diagnosis line up with what a reader would expect to
    see named.

    A binding whose driver fails contributes its failure and nothing else. One
    component's broken binding must never empty a surface a hundred other
    components are rendering fine (§2.3).
    """
    context = dict(context or {})
    by_driver: dict[str, list[DriverResult]] = {}

    for descriptor in descriptors:
        for binding in descriptor.bindings:
            module = DRIVERS.get(binding.driver)
            if module is None:
                # Unreachable in practice: `descriptor.parse` rejects an
                # unknown driver at build time (§2.7). Kept because a resolver
                # that trusts an upstream check is a resolver that breaks
                # silently when the check moves.
                result = DriverResult.failed(
                    binding, f"driver {binding.driver!r} is not registered")
            else:
                result = module.read(binding, context)
            by_driver.setdefault(binding.driver, []).append(result)

    return [_fold(driver, results) for driver, results in sorted(by_driver.items())]


def _fold(driver: str, results: list[DriverResult]) -> AdapterResult:
    """One driver's results for every component that bound to it.

    Status is OK when ANY binding succeeded. A driver marked FAILED renders all
    of its entities `UNREPORTED` (`index/graph.py`), and doing that because one
    component of fifty has a bad key would blank forty-nine components that are
    fine — an availability failure manufactured out of a configuration typo.
    The individual failures are still visible: they are in `unavailable`, and
    `doctor` names the specific binding.
    """
    ok = [r for r in results if r.ok]
    entities = tuple(e for r in ok for e in r.entities)
    edges = tuple(e for r in ok for e in r.edges)
    cadences = [r.cadence_seconds for r in ok
                if r.cadence_seconds and r.cadence_seconds > 0]
    failures = tuple(
        f"{r.binding.component_id}: {r.error}" for r in results if not r.ok
    )
    return AdapterResult(
        name=f"driver:{driver}",
        status=AdapterStatus.OK if (ok or not results) else AdapterStatus.FAILED,
        claim_class=ClaimClass.OBSERVATION,
        entities=entities,
        edges=edges,
        unavailable=failures,
        declared_cadence_seconds=min(cadences) if cadences else None,
    )
