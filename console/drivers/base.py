"""Drivers read a source SHAPE; descriptors say which one and where (§2.7).

§2.3's adapter is a *source* reader — it enumerates everything under a prefix
and the console's own config says which prefix. A **driver** is the same
capability named from the descriptor's side: a component says
`{driver: object-store, key: …}` and the driver knows how to read that shape.

The distinction is not cosmetic. It is where the cost of onboarding lives:

- **Adapter-shaped**, the binding is in the console's configuration, which is
  per-source. A component whose data lands somewhere new costs a console edit.
- **Driver-shaped**, the binding is in the component's own descriptor. A
  component whose data lands somewhere new costs nothing, because the thing
  that knows where it is, is the thing that put it there.

One driver per source *shape* — object store, state machine, local unit, log
location, query, emitted envelope — and a fleet of any size needs a handful,
because shapes are few and instances are many.

**A driver never knows a component.** Which bucket, group, ARN or table belongs
to whom is in the descriptor. A literal naming an instance in driver source is
a build finding, asserted by a lint (`tests/test_descriptor_bindings.py`), and
it is the same rule §2.3 applies to adapters one layer up.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..model.entity import Edge, Entity
from ..model.descriptor import Binding


class Cost(enum.Enum):
    """What reading this source costs, declared by the driver (§2.7).

    The console reads on the refresh pass (§5.9), never per request — but that
    is only sufficient if the cost is *known*. A driver that scans billed bytes
    on every rebuild is a different proposition from one that stats an object,
    and the difference has to be visible before somebody points a descriptor at
    a hundred components.
    """

    FREE = "free"          # a local file, an in-memory lookup
    CHEAP = "cheap"        # a metadata call: head/list/describe
    METERED = "metered"    # billed by volume scanned or rows returned
    SLOW = "slow"          # seconds-scale, poll-based


@dataclass(frozen=True)
class DriverResult:
    """What one driver returns for one binding.

    ``ok`` false is a *state*, not an exception (§2.3): the entity still
    renders, carrying what the driver could establish, and `error` says what
    failed. `doctor` (§3.9) names the binding, which is why bindings are
    individually identified rather than merged into one blob.
    """

    binding: Binding
    ok: bool = True
    entities: tuple[Entity, ...] = ()
    edges: tuple[Edge, ...] = ()
    unavailable: tuple[str, ...] = ()
    error: str | None = None
    #: Freshness promise this binding declares, feeding §5.9's shortest-cadence
    #: rule. None means the binding said nothing, and staleness is then not
    #: computable for it — which the surface states rather than assuming fresh.
    cadence_seconds: float | None = None

    @classmethod
    def failed(cls, binding: Binding, error: str,
               unavailable: tuple[str, ...] = ()) -> "DriverResult":
        return cls(binding=binding, ok=False, error=error,
                   unavailable=unavailable or ("source",))


class Driver(Protocol):
    """The contract every driver satisfies."""

    #: The shape name a descriptor writes as `driver:`.
    name: str

    #: Entity kinds this shape can produce.
    kinds: tuple[str, ...]

    #: What reading it costs (§2.7).
    cost: Cost

    def read(self, binding: Binding, context: dict[str, Any]) -> DriverResult:
        """Read this binding's source and return what it establishes.

        ``binding.spec`` is the component's own declaration — the only place an
        instance appears. ``context`` carries console-level facilities a driver
        may need (a clock, an injected reader for tests) and never topology.

        Must not raise. An unreachable source is `DriverResult.failed`, because
        one component's broken binding must not empty a surface that a hundred
        other components are rendering fine (§2.3).
        """
        ...
