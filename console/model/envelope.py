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
SCHEMA_VERSION = 1


class AdapterStatus(enum.Enum):
    """Whether the adapter's source was reachable this pass (§2.3)."""

    OK = "ok"
    FAILED = "failed"  # source unreachable — entities render UNREPORTED


@dataclass(frozen=True)
class AdapterResult:
    """What one adapter returns from one pass over its source.

    ``name`` is the adapter's configured name (config.example.yaml `name:`).
    ``entities``/``edges`` are the projection; ``unavailable`` is the honest
    declaration of what the source cannot supply; ``status`` is FAILED when
    the source could not be reached, in which case ``entities`` carries the
    source's known entities rendered UNREPORTED (never an empty surface).
    """

    name: str
    status: AdapterStatus
    entities: tuple[Entity, ...] = ()
    edges: tuple[Edge, ...] = ()
    # Names of facets/fields this source cannot supply (e.g. "baseline",
    # "cost", "as_of"). Rendered as declared absence, never guessed (§2.3).
    unavailable: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION
