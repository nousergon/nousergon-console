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
from .cadence_state import resolve_cadence_state
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
        # The union of every OK discovery pass's declared scope, as (facet,
        # value) pairs. `_saw_fleetwide_discovery` is the separate flag for a
        # pass that declared no scope at all — it claims the whole population,
        # so it is not a member of this set and must not be modelled as one.
        self._discovery_scope: set[tuple[str, str]] = set()
        self._saw_fleetwide_discovery = False
        self._saw_ok_declaration = False
        # §8.3's DISABLED/MISSED separation is a comparison between a declared
        # cadence and an observed silence, so it needs the same tolerance
        # `staleness_honesty` uses — one factor, set from configuration, never
        # two that can disagree. See `index/cadence_state.py`.
        self._staleness_factor = 1.5
        self._finalized = False
        self._entities: dict[str, Entity] = {}
        self._liveness_watcher: str | None = None
        self._by_kind: dict[Kind, list[Entity]] = {k: [] for k in Kind}
        # Forward and reverse adjacency, keyed by entity id. Reverse edges are
        # derived at ingest from the forward declarations (§3.3).
        self._out: dict[str, list[Edge]] = {}
        self._in: dict[str, list[Edge]] = {}
        self._declared_registries: set[str] = set()
        self._rendered_registries: set[str] = set()
        # §9.1's per-registry row count, keyed by registry name — populated by
        # config.build_index right after each registry adapter runs. Separate
        # from _rendered_registries (§7's PAGE coverage): a registry can have
        # a generated page and still have failed to read this pass.
        self._registry_rows: dict[str, dict[str, object]] = {}
        # §9.4/§9.8 are computed once per build (a question-set run and a git
        # subprocess are both too costly to redo per request) and cached here
        # by config.build_index — see set_answer_latency/set_onboarding_cost.
        self._answer_latency: dict[str, object] = {
            "computable": False,
            "reason": "not computed for this build — built directly via Index() "
                      "rather than console.config.build_index",
        }
        self._onboarding_cost: dict[str, object] = {
            "count": None, "of": 0, "computable": False,
            "reason": "not computed for this build — built directly via Index() "
                      "rather than console.config.build_index",
        }

    def set_staleness_factor(self, factor: float) -> None:
        """The multiplier on a declared cadence before silence is a defect.

        Set by `config.build_index` from `console.staleness_factor`, the same
        value `staleness_honesty()` is called with — a merge that placed a row
        `HEALTHY` on one tolerance while §9.6 called it a violation on another
        would be two numbers disagreeing about one row.
        """
        self._staleness_factor = float(factor)
        self._finalized = False

    def declare_registry(self, name: str) -> None:
        """Record a configured registry even when its adapter cannot build it."""
        self._declared_registries.add(name)

    def render_registry(self, name: str) -> None:
        self._rendered_registries.add(name)

    def record_registry_rows(self, name: str, count: int, ok: bool) -> None:
        """§9.1's per-registry denominator: how many rows this registry
        offered this pass, and whether the adapter could read it at all."""
        self._registry_rows[name] = {"count": count, "ok": ok}

    def set_answer_latency(self, value: dict[str, object]) -> None:
        self._answer_latency = value

    def answer_latency(self) -> dict[str, object]:
        """§9.4 — the last question-set run against this build."""
        return self._answer_latency

    def set_liveness_watcher(self, component_id: str | None) -> None:
        """Declare which component watches this surface from outside it (§9.7).

        The id comes from configuration, never from a literal here: which
        component plays this role is a fleet fact, and §2.3 keeps fleet facts
        out of this repo.
        """
        self._liveness_watcher = component_id

    def surface_liveness(self) -> dict[str, object]:
        """§9.7 — is this surface up, according to something that is not it?

        The rule this discharges is that **a surface cannot notice its own
        absence**, so the console must never compute this from its own uptime.
        It does not: it RENDERS the verdict of an external watcher that reached
        the index like any other component, through an adapter, from a durable
        artifact the watcher wrote. If the console is dark, nothing here renders
        at all — which is exactly why the watcher's own alerting path, not this
        number, is what reaches a human. This number answers the different and
        also-necessary question: *is anything watching, and what did it last
        say?*

        Three outcomes, deliberately distinct (§5.5):

        - no watcher declared    → `N/A-NOT-IMPL`, naming what would fix it
        - declared but absent    → `UNREPORTED`, which is a finding: a declared
                                   watcher missing from the index means the
                                   watcher is not running, or its adapter is
                                   not configured, and both are worse than
                                   having declared nothing
        - present                → its state, as-of and evidence link
        """
        watcher = getattr(self, "_liveness_watcher", None)
        if not watcher:
            return {
                "state": "N/A-NOT-IMPL",
                "watcher": None,
                "reason": (
                    "no liveness watcher declared — set `console.liveness_watcher` "
                    "to the component id of a watcher that runs OFF this surface "
                    "(§9.7); a surface cannot notice its own absence"
                ),
            }
        entity = self.entity(watcher)
        if entity is None:
            return {
                "state": State.UNREPORTED.value,
                "watcher": watcher,
                "reason": (
                    f"{watcher!r} is declared as this surface's liveness watcher "
                    "but no adapter produced it — the watcher is not running, or "
                    "nothing is configured to read what it writes"
                ),
            }
        return {
            "state": entity.state.value,
            "watcher": watcher,
            "as_of": entity.provenance.as_of if entity.provenance else None,
            "source": entity.provenance.source if entity.provenance else None,
            "evidence": f"/component/{watcher}",
        }

    def set_onboarding_cost(self, value: dict[str, object]) -> None:
        self._onboarding_cost = value

    def onboarding_cost(self) -> dict[str, object]:
        """§9.8 — the last onboarding-cost computation for this build."""
        return self._onboarding_cost

    def registry_coverage(self) -> dict[str, object]:
        """The denominator-backed generated-page coverage required by §7."""
        missing = sorted(self._declared_registries - self._rendered_registries)
        return {"count": len(self._rendered_registries),
                "of": len(self._declared_registries), "missing": missing}

    def registry_names(self) -> list[str]:
        return sorted(self._rendered_registries)

    def broken_registries(self) -> list[str]:
        """Declared registries this pass could not read, or never reached.

        Shared by §9.1 and §9.6 because both are aggregates over a population
        the registry defines: a declared registry that failed removes rows from
        the denominator invisibly, and §5.3 forbids an aggregate over
        incomplete input. §9.6 grew this guard after a live tick reported
        `{count: 0, of: 0}` — a count over an empty population, rendered
        indistinguishably from "all clear" (alpha-engine-config-I7126).
        """
        unread = self._declared_registries - set(self._registry_rows)
        failed = {n for n, info in self._registry_rows.items() if not info["ok"]}
        return sorted(unread | failed)

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
                if result.discovery_scope:
                    self._discovery_scope.update(result.discovery_scope)
                else:
                    self._saw_fleetwide_discovery = True
            elif result.claim_class is ClaimClass.DECLARATION:
                self._saw_ok_declaration = True
        for ent in result.entities:
            ent = _stamp_source_cadence(ent, result.declared_cadence_seconds)
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
            if ent.state is State.UNREPORTED and self._within_discovery_scope(ent):
                return dataclasses.replace(ent, state=State.ABSENT)
        # The third state that exists only as a comparison between claims:
        # `MISSED` (and the `HEALTHY` on its other side) for a component a
        # counter-reading source found SILENT and a registry row declares a
        # cadence for. Disjoint from the ABSENT branch above by construction —
        # that one requires no discovery claim, this one requires a claim that
        # actually read a window. See `index/cadence_state.py`.
        return resolve_cadence_state(ent, staleness_factor=self._staleness_factor)

    def _within_discovery_scope(self, ent: Entity) -> bool:
        """Could any successful discovery pass have found this entity?

        The third guard on `ABSENT`, alongside "a registry declared it" and "a
        discovery adapter ran fine". A pass that enumerated one substrate
        (`AdapterResult.discovery_scope`) speaks only for entities in that
        substrate; everything else it simply did not look at, and "I did not
        look" must not render as "it is not there". Without this, the FIRST
        substrate-scoped discovery adapter flips every unobserved row in the
        fleet — other substrates included — from `UNREPORTED` to `ABSENT`,
        replacing a true transparency gap with a false absence claim.

        A pass declaring no scope claims the whole population, which is the
        behaviour every existing discovery adapter already has.
        """
        if self._saw_fleetwide_discovery:
            return True
        return any(
            ent.facets.get(facet) == value
            for facet, value in self._discovery_scope
        )

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

    # ---- §9.1 / §9.2 completeness ------------------------------------------

    def population_completeness(self) -> dict[str, object]:
        """§9.1 — registry rows RENDERED ÷ registry rows, plus the separate
        UNREGISTERED count (`observability-policy.md` §8.4).

        "Registry rows" is every identifier that received a DECLARATION claim
        for a Component (§2.4 — the registry is the denominator, never a
        second console-side list). "Rendered" is how many of those
        identifiers are actually present in the finalized index: the merge
        never drops a claimed id today (`NamespaceCollision` aside), so this
        is a real set comparison rather than an assertion, and it is what
        would catch a future merge path that DOES drop one.

        Uncomputable — `of`/`ratio` both `None`, never the reserved
        `N/A-NOT-IMPL` token; §11's carve-out does not cover §9.1 — with no
        successful declaration pass at all, OR when any registry that WAS
        declared could not be read this pass: a ratio computed only over the
        registries that happened to work would silently pretend the broken
        ones do not exist (§5.3 — no aggregate over incomplete input). The
        second case is the narrower one this method adds on top of the
        original "no registry configured" check: `_saw_ok_declaration` alone
        goes true the moment ONE of several configured registries succeeds,
        which would otherwise report a ratio as if a second, failed registry
        did not exist.

        Both counts publish their members — `unregistered_ids` beside
        `unregistered`, `unrendered_ids` beside `rendered`/`of` — so the number
        is actionable from the surface rather than re-derived by hand on the
        box (alpha-engine-config-I7107; the same treatment §9.6's `violations`
        received in `nousergon-console-PR86`). The lists are always present,
        empty when the count is 0: a key that appears only on failure makes
        every consumer write the absent-key branch, and a consumer that skips
        it reads a healthy surface as a schema error.
        """
        self.finalize()
        # §2.4 / alpha-engine-config-I6970: the registry declares COMPONENT
        # rows only, never RUN rows — a checks-envelope adapter mints a run's
        # id as `f"{check_id}@{ran_at}"` (console/adapters/checks_envelope.py),
        # which never matches its own component's registry row and would
        # otherwise be counted UNREGISTERED once per run instead of zero times
        # ever. Comparing every RUN-kind entity against the registry inflated
        # this count 8.5x (15 of 17) against the live fleet registry: the 15
        # were runs of already-declared components, not registry gaps.
        # Both counts NAME their members (§5.1's evidence field, §3.1's three
        # reachability paths), for the reason `render/json.py::_named_members`
        # gives for §9.6: a count whose population cannot be enumerated reports
        # a defect nobody can locate. Measured cost of not doing it here:
        # `unregistered` moved 0 -> 1 on the live surface between 19:45Z and
        # 21:45Z on 2026-08-12 and four separate probes over SSM could not name
        # the row (alpha-engine-config-I7107). `unrendered` is the same defect
        # on the other count — a declared id the merge dropped is by
        # construction absent from every view, so the ratio would be the only
        # trace it ever existed.
        unregistered_ids = sorted(
            e.id for e in self._entities.values()
            if e.state is State.UNREGISTERED and e.kind is Kind.COMPONENT
        )
        unregistered = len(unregistered_ids)
        broken = self.broken_registries()
        if not self._saw_ok_declaration or broken:
            return {"rendered": 0, "of": None, "ratio": None,
                    "unregistered": unregistered,
                    "unregistered_ids": unregistered_ids,
                    "unrendered_ids": []}
        declared_ids = {
            entity_id for entity_id, claims in self._claims.items()
            if any(
                c.claim_class is ClaimClass.DECLARATION
                and c.entity.kind is Kind.COMPONENT
                for c in claims
            )
        }
        unrendered_ids = sorted(eid for eid in declared_ids if eid not in self._entities)
        rendered = len(declared_ids) - len(unrendered_ids)
        of = len(declared_ids)
        return {
            "rendered": rendered, "of": of,
            "ratio": round(rendered / of, 4) if of else None,
            "unregistered": unregistered,
            "unregistered_ids": unregistered_ids,
            "unrendered_ids": unrendered_ids,
        }

    def transparency_gap(self) -> dict[str, object]:
        """§9.2 — components in UNREPORTED, over the population that could
        BE `UNREPORTED` (Component/Run), never every entity kind (§9's
        original defect: an Artifact or Decision is never a transparency-gap
        candidate, and folding it into the denominator only dilutes it).
        """
        self.finalize()
        population = [
            e for e in self._entities.values() if e.kind in COMPONENT_STATE_KINDS
        ]
        unreported = [e for e in population if e.state is State.UNREPORTED]
        return {"count": len(unreported), "of": len(population)}

    # ---- §9.5 / §9.6 --------------------------------------------------------

    def orphan_counts(self) -> dict[str, object]:
        """§9.5 — panes with no kind, and kinds with no pane."""
        from .numbers import orphan_counts as _orphan_counts

        return _orphan_counts(self)

    def staleness_honesty(self, now=None, staleness_factor: float = 1.5) -> dict[str, object]:
        """§9.6 — rows older than their own declared cadence with no marker."""
        from .numbers import staleness_honesty as _staleness_honesty

        return _staleness_honesty(self, now=now, staleness_factor=staleness_factor)

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


def _stamp_source_cadence(ent: Entity, cadence_seconds: float | None) -> Entity:
    """Carry the SOURCE's own poll cadence onto the claim's provenance.

    Done here, once, for every adapter, rather than at each adapter's call
    site: `AdapterResult.declared_cadence_seconds` is already the one place a
    source states how often it is re-read (§5.9 uses it for the whole
    surface's as-of), and a second per-entity spelling of the same number is a
    second thing to keep in step.

    Why any consumer needs it: an `as_of` is when the source last SAW the
    fact, so its age carries the observer's polling lag as well as the
    subject's own. Comparing that age against a declared cadence without the
    observer's term measures the phase offset between two schedules — which is
    exactly what made §9.6 flap 0<->2 on the live surface with nothing
    changing (alpha-engine-config-I7126).

    An adapter that already stated a cadence on its own provenance keeps it —
    nothing here overwrites a more specific claim.
    """
    if cadence_seconds is None or ent.provenance.cadence_seconds is not None:
        return ent
    return dataclasses.replace(
        ent, provenance=dataclasses.replace(
            ent.provenance, cadence_seconds=float(cadence_seconds)),
    )


def _as_unreported(ent: Entity) -> Entity:
    """Render a claim from a FAILED adapter as UNREPORTED (§2.3, §5.5).

    Applied to the CLAIM, before merge — so an unreachable source contributes
    an honest "I could not see this" rather than blanking an entity that three
    other sources reported on fine.
    """
    if ent.kind in COMPONENT_STATE_KINDS:
        return dataclasses.replace(ent, state=State.UNREPORTED)
    return dataclasses.replace(ent, state="unreported-by-source")
