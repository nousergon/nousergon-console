"""The versioned adapter envelope — the contract's return shape.

Every adapter returns one ``AdapterResult``: the entities and edges it read,
plus an honest declaration of what it could not supply and whether its source
was reachable at all. This is `console-policy.md` §2.3 made mechanical:

- **An adapter declares what it cannot supply** — ``unavailable`` names the
  facets/fields the source has no value for, so the entities carry the
  corresponding state rather than a silent default.
- **A failing adapter is an entity state, not an exception** — when the source
  is unreachable the adapter returns ``status=FAILED`` with its entities
  marked UNREPORTED; it never raises the surface empty and never drops rows.

The envelope is versioned from birth (``SCHEMA_VERSION``). The index, the
relation graph and every future adapter depend on this shape, so it carries a
version a consumer can assert rather than drifting silently.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .entity import Edge, Entity

#: Bumped on any breaking change to the envelope shape. Consumers assert
#: equality; a mismatch is a loud failure at ingest, not a mis-parsed entity.
SCHEMA_VERSION = 3


class AdapterStatus(enum.Enum):
    """Whether the adapter's source was reachable this pass (§2.3)."""

    OK = "ok"
    FAILED = "failed"  # source unreachable — entities render UNREPORTED


class ClaimClass(enum.Enum):
    """What KIND of statement this adapter's source is making (§2.5).

    Several sources describing one entity is the normal case, not a collision:
    a registry *declares* a component, telemetry *observes* it, a substrate
    enumeration *discovers* it. The index merges their claims by identifier,
    and this is the precedence it merges under — lower rank wins a contested
    field.

    The ordering is not a quality judgement. It is about **what each kind of
    source is in a position to know.** A registry is the only thing that can
    say a component is deliberately off; telemetry is the only thing that can
    say it ran; an enumeration can only say it is there.
    """

    DECLARATION = 1  # a registry: existence, lifecycle, owner, authority tier
    OBSERVATION = 2  # telemetry: state, as-of, run history, counts
    DISCOVERY = 3    # a substrate enumeration: existence, and little else

    @property
    def rank(self) -> int:
        return self.value


@dataclass(frozen=True)
class AdapterResult:
    """What one adapter returns from one pass over its source.

    ``name`` is the adapter's configured name (config.example.yaml `name:`).
    ``claim_class`` is what kind of statement this source makes (§2.5) and
    decides precedence when another adapter describes the same identifier.
    ``entities``/``edges`` are the projection; ``unavailable`` is the honest
    declaration of what the source cannot supply; ``status`` is FAILED when
    the source could not be reached, in which case ``entities`` carries the
    source's known entities rendered UNREPORTED (never an empty surface) —
    and that downgrade applies to THIS adapter's claim only, never to a merged
    entity three other sources can still see (§2.5).
    """

    name: str
    status: AdapterStatus
    # What kind of statement this source makes (§2.5). Declared by the adapter,
    # never inferred by the index — an adapter knows whether it is reading a
    # declaration or watching a substrate, and the index cannot tell from the
    # entities alone. Defaults to OBSERVATION because that is the honest answer
    # for a source that has not said: it is the middle rank, so it neither
    # usurps a registry's lifecycle nor loses to a bare enumeration.
    claim_class: ClaimClass = ClaimClass.OBSERVATION
    entities: tuple[Entity, ...] = ()
    edges: tuple[Edge, ...] = ()
    # Names of facets/fields this source cannot supply (e.g. "baseline",
    # "cost", "as_of"). Rendered as declared absence, never guessed (§2.3).
    unavailable: tuple[str, ...] = ()
    # When this source was actually read (ISO-8601), and how often it is
    # expected to change. Both feed §5.9: the index's own as-of bounds every
    # row's, and a render pass older than the SHORTEST declared cadence among
    # its sources is stale — whole-surface, because it is one fact about one
    # index. An adapter that cannot say declares None rather than guessing.
    fetched_at: str | None = None
    declared_cadence_seconds: float | None = None
    # Component descriptors this source declared (§2.6). Only a registry
    # adapter populates it. Carried on the result rather than read by the
    # adapter itself, because walking a descriptor's bindings means reaching
    # other sources — which §2.3 forbids an adapter from doing.
    descriptors: tuple = ()
    schema_version: int = SCHEMA_VERSION
