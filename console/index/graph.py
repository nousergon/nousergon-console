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

import dataclasses

from ..model.entity import RELATIONS, Edge, Entity
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass
from ..model.kinds import COMPONENT_STATE_KINDS, Kind, State
from .build import AdapterFetch, BuildInfo
from .merge import Claim, NamespaceCollision, merge

__all__ = ["Index", "NamespaceCollision"]


class Index:
    """The derived entity graph. Build once per pass from AdapterResults.

    Claims are collected as they arrive and resolved in `finalize()` (§2.5), so
    the result never depends on adapter ordering — a surface that renders
    differently because two adapters raced is a surface nobody can reason about.
    """

    def __init__(self) -> None:
        self._claims: dict[str, list[Claim]] = {}
        # §5.9: the index has an as-of, and it bounds every row's. `built_at`
        # is replaced by the supervisor when it stamps a completed build; the
        # empty default is what an un-stamped index honestly reports.
        self.build_info = BuildInfo(built_at="")
        self._saw_ok_discovery = False
        self._saw_ok_declaration = False
        self._finalized = False
        self._entities: dict[str, Entity] = {}
        self._by_kind: dict[Kind, list[Entity]] = {k: [] for k in Kind}
        # Forward and reverse adjacency, keyed by entity id. Reverse edges are
        # derived at ingest from the forward declarations (§3.3).
        self._out: dict[str, list[Edge]] = {}
        self._in: dict[str, list[Edge]] = {}

    # ---- ingest -----------------------------------------------------------

    def add_result(self, result: AdapterResult) -> None:
        """Record one adapter's claims (§2.5). Resolution happens in finalize().

        A FAILED adapter still contributes its entities, rendered UNREPORTED —
        absence renders as itself (§5.5), never as a vanished row. That downgrade
        applies to **this adapter's claim only**: a merged entity three other
        sources can still see is not blanked because one source was unreachable.
        """
        # Any new claim invalidates a previous resolution — otherwise an
        # adapter added after a query silently never appears, which is the
        # quietest possible way to lose a source.
        self._finalized = False
        # Every source's read is recorded whether it worked or not — an
        # adapter that failed is a fact about the surface's completeness, and
        # dropping it here would make the failure invisible on the page.
        self.build_info = dataclasses.replace(
            self.build_info,
            adapters=self.build_info.adapters + (AdapterFetch.of(result),),
        )
        if result.status is AdapterStatus.OK:
            if result.claim_class is ClaimClass.DISCOVERY:
                self._saw_ok_discovery = True
            elif result.claim_class is ClaimClass.DECLARATION:
                self._saw_ok_declaration = True
        for ent in result.entities:
            ent = ent if result.status is AdapterStatus.OK else _as_unreported(ent)
            self._claims.setdefault(ent.id, []).append(
                Claim(
                    entity=ent,
                    claim_class=result.claim_class,
                    adapter=result.name,
                    reachable=result.status is AdapterStatus.OK,
                )
            )
        for edge in result.edges:
            self._add_edge(edge)

    def finalize(self) -> "Index":
        """Resolve every identifier's claims into one entity (§2.5).

        Idempotent, and called lazily by every query, so a caller that forgets
        it gets a correct index rather than an empty one — the failure mode of
        a two-phase build is a surface that renders nothing and says nothing.
        """
        if self._finalized:
            return self
        self._entities = {}
        self._by_kind = {k: [] for k in Kind}
        for entity_id, claims in self._claims.items():
            ent = merge(claims)
            ent = self._reconcile(ent, claims)
            self._entities[entity_id] = ent
            self._by_kind[ent.kind].append(ent)
        self._finalized = True
        return self

    def _reconcile(self, ent: Entity, claims: list[Claim]) -> Entity:
        """The two states that exist only as a comparison BETWEEN claims (§8.3).

        Neither is computable by any adapter alone, which is the whole reason
        the merge had to exist before they could be rendered:

        - `UNREGISTERED` — something reported on it (a substrate enumeration
                           OR an emitted report — both are evidence it is
                           running) and no registry declared it. Requires a
                           *successful* declaration pass: with no registry
                           configured at all there is no denominator, so
                           "unregistered" is not a claim anyone can make, and
                           making it would paint an entire registry-less
                           surface red on a configuration choice.
        - `ABSENT`       — declared, and a discovery adapter that ran fine did
                           not find it. Requires a *successful* discovery pass:
                           without one, absence is unobserved rather than
                           established, and reporting it would be the
                           absence-of-evidence read §8.3 forbids.

        Both guards are the same shape, and it is the shape §8.3 asks for: a
        state whose meaning IS absence may only be asserted by a check that
        actually looked.
        """
        if ent.kind not in COMPONENT_STATE_KINDS:
            return ent
        classes = {c.claim_class for c in claims}
        declared = ClaimClass.DECLARATION in classes
        discovered = ClaimClass.DISCOVERY in classes
        if not declared and self._saw_ok_declaration:
            return dataclasses.replace(ent, state=State.UNREGISTERED)
        if declared and not discovered and self._saw_ok_discovery:
            if ent.state is State.UNREPORTED:
                return dataclasses.replace(ent, state=State.ABSENT)
        return ent

    def conflicts(self) -> list[Entity]:
        """§9.9 — entities carrying an unresolved equal-rank disagreement.

        Published rather than suppressed: a conflict is two sources disagreeing
        about the fleet, which is a fact about the fleet and not a rendering
        problem.
        """
        return [e for e in self.finalize()._entities.values() if e.conflicts]

    def _add_edge(self, edge: Edge) -> None:
        if edge.rel not in RELATIONS:
            raise ValueError(f"unknown relation predicate {edge.rel!r} (§3.3)")
        self._out.setdefault(edge.source, []).append(edge)
        # The reverse edge is derived here, once, from the forward declaration.
        reverse = Edge(source=edge.target, rel=RELATIONS[edge.rel], target=edge.source)
        self._in.setdefault(edge.target, []).append(reverse)

    # ---- queries ----------------------------------------------------------

    def entity(self, entity_id: str) -> Entity | None:
        return self.finalize()._entities.get(entity_id)

    def of_kind(self, kind: Kind) -> list[Entity]:
        return list(self.finalize()._by_kind[kind])

    def all(self) -> list[Entity]:
        return list(self.finalize()._entities.values())

    def edges(self) -> list[Edge]:
        """Every forward edge declared across the ingested set (§3.3).

        The reverse edges are derived and never stored; this returns the
        forward declarations only, in ingest order — the projection a dump
        or consumer needs to reproduce the relation graph.
        """
        return [e for out in self._out.values() for e in out]

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
        self.finalize()
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
    """Render a claim from a FAILED adapter as UNREPORTED (§2.3, §5.5).

    Applied to the CLAIM, before merge — so an unreachable source contributes
    an honest "I could not see this" rather than blanking an entity that three
    other sources reported on fine.
    """
    if ent.kind in COMPONENT_STATE_KINDS:
        return dataclasses.replace(ent, state=State.UNREPORTED)
    return dataclasses.replace(ent, state="unreported-by-source")
