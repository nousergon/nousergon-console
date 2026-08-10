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

__all__ = ["Index", "NamespaceCollision", "DECISION_QUEUE_LABELS"]

#: The Decision Queue's own labels (`decision-queue-policy.md` §2) — the
#: escalation terminal and the ready-for-ruling state. A Decision entity is any
#: ruling, gate, queued reserved matter or open ASK (`console-policy.md`
#: §2.1); most of them are ordinary open backlog rather than something
#: reserved for Brian, so "what is waiting on Brian" (§4.3) is this narrower
#: set, not every open Decision.
DECISION_QUEUE_LABELS: frozenset[str] = frozenset({"gate:decision", "triage:session"})


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
        self._declared_registries: set[str] = set()
        self._rendered_registries: set[str] = set()

    def declare_registry(self, name: str) -> None:
        """Record a configured registry even when its adapter cannot build it."""
        self._declared_registries.add(name)

    def render_registry(self, name: str) -> None:
        self._rendered_registries.add(name)

    def registry_coverage(self) -> dict[str, object]:
        """The denominator-backed generated-page coverage required by §7."""
        missing = sorted(self._declared_registries - self._rendered_registries)
        return {"count": len(self._rendered_registries),
                "of": len(self._declared_registries), "missing": missing}

    def registry_names(self) -> list[str]:
        return sorted(self._rendered_registries)

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

    def decision_queue(self) -> list[Entity]:
        """§4.3 — what is waiting on Brian.

        Filtered to `DECISION_QUEUE_LABELS`, not every open Decision: a
        Decision entity is any ruling, gate, queued reserved matter or open
        ASK (§2.1), and most open issues on a tracker are ordinary backlog
        rather than something reserved for Brian's ruling. Only the two
        canonical `decision-queue-policy.md` §2 labels mean the latter.
        """
        return [
            e for e in self.finalize()._by_kind[Kind.DECISION]
            if DECISION_QUEUE_LABELS & set(e.detail.get("labels") or ())
        ]

    def population_completeness(self) -> dict[str, object]:
        """§9.1 — registry rows rendered ÷ registry rows, and the
        UNREGISTERED count (`observability-policy.md` §8.4's two numbers).

        "Registry rows" is every identifier that received a DECLARATION claim
        for a Component (§2.4 — the registry is the denominator, never a
        second console-side list). "Rendered" is how many of those
        identifiers are actually present in the finalized index: the merge
        never drops a claimed id today (`NamespaceCollision` aside), so this
        is a real set comparison rather than an assertion, and it is what
        would catch a future merge path that DOES drop one.

        With no successful declaration pass there is no denominator, and a
        ratio computed without one is a fabricated 1.0 — `config.example.yaml`
        documents the honest behaviour: "reports population completeness as
        unknown rather than as 1.0". `ratio` and `of` are both `None` in that
        case, distinct from a real `0 / 0`.
        """
        self.finalize()
        unregistered = sum(
            1 for e in self._entities.values() if e.state is State.UNREGISTERED
        )
        if not self._saw_ok_declaration:
            return {"rendered": 0, "of": None, "ratio": None,
                    "unregistered": unregistered}
        declared_ids = {
            entity_id for entity_id, claims in self._claims.items()
            if any(
                c.claim_class is ClaimClass.DECLARATION
                and c.entity.kind is Kind.COMPONENT
                for c in claims
            )
        }
        rendered = sum(1 for eid in declared_ids if eid in self._entities)
        of = len(declared_ids)
        return {
            "rendered": rendered, "of": of,
            "ratio": round(rendered / of, 4) if of else None,
            "unregistered": unregistered,
        }

    def _add_edge(self, edge: Edge) -> None:
        if edge.rel not in RELATIONS:
            raise ValueError(f"unknown relation predicate {edge.rel!r} (§3.3)")
        self._out.setdefault(edge.source, []).append(edge)
        # The reverse edge is derived here, once, from the forward declaration.
        reverse = Edge(source=edge.target, rel=RELATIONS[edge.rel], target=edge.source)
        self._in.setdefault(edge.target, []).append(reverse)

    # ---- queries ----------------------------------------------------------

    def claims_for(self, entity_id: str) -> list[Claim]:
        """Every claim made about this identifier, resolved or not (§3.9).

        Exposed for `doctor`: diagnosing an absence needs what the sources
        SAID, which the merged entity has already collapsed. An identifier with
        claims and no entity is a different finding from one with neither, and
        they have different fixes.
        """
        return list(self._claims.get(entity_id, []))

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

    def inbound(self, entity_id: str) -> list[Edge]:
        """The DERIVED reverse edges at this entity — "what points at me".

        Distinct from `related()`, which mixes both directions: relation-
        reachability (§3.1, §9.3) is specifically about being pointed AT, since
        an entity that only points outward is still findable only by someone
        who already knows it exists.
        """
        return list(self._in.get(entity_id, []))

    def related(self, entity_id: str) -> list[Edge]:
        """Every edge touching this entity, both directions (§3.3).

        The reverse direction is the load-bearing one: "who breaks if this is
        stale" is answered from ``_in`` and exists nowhere unless the index
        derives it.
        """
        return list(self._out.get(entity_id, [])) + list(self._in.get(entity_id, []))

    # ---- §9.3 reachability ------------------------------------------------

    def reachability(self) -> dict[str, object]:
        """§9.3 — execute all three §3.1 paths and publish each denominator."""
        from .reachability import measure

        return measure(self)


def _as_unreported(ent: Entity) -> Entity:
    """Render a claim from a FAILED adapter as UNREPORTED (§2.3, §5.5).

    Applied to the CLAIM, before merge — so an unreachable source contributes
    an honest "I could not see this" rather than blanking an entity that three
    other sources reported on fine.
    """
    if ent.kind in COMPONENT_STATE_KINDS:
        return dataclasses.replace(ent, state=State.UNREPORTED)
    return dataclasses.replace(ent, state="unreported-by-source")
