"""The entity index — one typed graph over every adapter's projection.

The index is the only place cross-source relations form (`console-policy.md`
§2.3): adapters return entities and forward edges keyed by identifier, and the
index derives reverse edges so every relation is navigable from both ends
(§3.3). It is in-memory and rebuilt from the adapters on each pass — nothing
is persisted (§5.6); deriving rather than storing is what makes §5.6
structural instead of a promise.

Two obligations are enforced here, not left to renderers:

- **One namespace (§3.6).** An id collision across the ingested set is a build
  error — ``NamespaceCollision`` — never a silent shadow.
- **No silently-truncated surface.** An adapter that FAILED contributes its
  entities as UNREPORTED rows; they are present and marked, never dropped
  (§2.3, §5.5).
"""
from __future__ import annotations

from ..model.entity import RELATIONS, Edge, Entity
from ..model.envelope import AdapterResult, AdapterStatus
from ..model.kinds import Kind, State


class NamespaceCollision(Exception):
    """Two entities share an id (§3.6). The build fails rather than shadow."""


class Index:
    """The derived entity graph. Build once per pass from AdapterResults."""

    def __init__(self) -> None:
        self._entities: dict[str, Entity] = {}
        self._by_kind: dict[Kind, list[Entity]] = {k: [] for k in Kind}
        # Forward and reverse adjacency, keyed by entity id. Reverse edges are
        # derived at ingest from the forward declarations (§3.3).
        self._out: dict[str, list[Edge]] = {}
        self._in: dict[str, list[Edge]] = {}

    # ---- ingest -----------------------------------------------------------

    def add_result(self, result: AdapterResult) -> None:
        """Fold one adapter's projection into the graph.

        A FAILED adapter still contributes its entities, rendered UNREPORTED —
        absence renders as itself (§5.5), never as a vanished row.
        """
        for ent in result.entities:
            ent = (
                ent
                if result.status is AdapterStatus.OK
                else _as_unreported(ent)
            )
            self._add_entity(ent)
        for edge in result.edges:
            self._add_edge(edge)

    def _add_entity(self, ent: Entity) -> None:
        if ent.id in self._entities:
            raise NamespaceCollision(
                f"entity id {ent.id!r} ingested twice (§3.6 one namespace)"
            )
        self._entities[ent.id] = ent
        self._by_kind[ent.kind].append(ent)

    def _add_edge(self, edge: Edge) -> None:
        if edge.rel not in RELATIONS:
            raise ValueError(f"unknown relation predicate {edge.rel!r} (§3.3)")
        self._out.setdefault(edge.source, []).append(edge)
        # The reverse edge is derived here, once, from the forward declaration.
        reverse = Edge(source=edge.target, rel=RELATIONS[edge.rel], target=edge.source)
        self._in.setdefault(edge.target, []).append(reverse)

    # ---- queries ----------------------------------------------------------

    def entity(self, entity_id: str) -> Entity | None:
        return self._entities.get(entity_id)

    def of_kind(self, kind: Kind) -> list[Entity]:
        return list(self._by_kind[kind])

    def all(self) -> list[Entity]:
        return list(self._entities.values())

    def related(self, entity_id: str) -> list[Edge]:
        """Every edge touching this entity, both directions (§3.3).

        The reverse direction is the load-bearing one: "who breaks if this is
        stale" is answered from ``_in`` and exists nowhere unless the index
        derives it.
        """
        return list(self._out.get(entity_id, [])) + list(self._in.get(entity_id, []))

    # ---- §9.3 reachability ------------------------------------------------

    def reachability(self) -> dict[str, object]:
        """§9.3 — entities reachable on all three §3.1 paths ÷ total.

        A pure graph property:
        - structure-reachable: the entity exists in the index (nav is generated
          from the index, so presence implies a generated nav path to it);
        - relation-reachable: at least one inbound edge;
        - search-reachable: computed by the search layer over the same ids —
          an id present here resolves by construction, so presence implies it.

        The number is published even when below 1.0 (§9) and names its
        denominator (§5.3).
        """
        total = len(self._entities)
        relation_reachable = sum(1 for eid in self._entities if self._in.get(eid))
        # Structure and search are implied by presence in the generated index;
        # the binding constraint for "all three" is the inbound edge.
        reachable_all_three = relation_reachable
        return {
            "total": total,
            "relation_reachable": relation_reachable,
            "reachable_all_three": reachable_all_three,
            "ratio": round(reachable_all_three / total, 4) if total else None,
        }


def _as_unreported(ent: Entity) -> Entity:
    """Render an entity from a FAILED adapter as UNREPORTED (§2.3, §5.5)."""
    import dataclasses

    return dataclasses.replace(ent, state=State.UNREPORTED)
