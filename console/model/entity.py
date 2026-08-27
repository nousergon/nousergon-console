"""Entity and Edge — the wire types every adapter returns.

An adapter is a function from configuration to entities and edges
(`console-policy.md` §2.3). These are the two types it may return, and the
only types the index ingests. Identity is assigned by the source of truth,
never console-minted (§2.1) — there is deliberately no id-generation here.

Every entity carries the §5.1 four-field row contract provenance: state,
source, as-of, evidence. A figure that cannot say how it knows is not yet
trustworthy, so the fields are required at construction, not optional at
render.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from .kinds import COMPONENT_STATE_KINDS, Kind, State


@dataclass(frozen=True)
class Provenance:
    """How a rendered fact knows what it claims (§5.1).

    - ``source``    — which durable artifact, registry, table or API this came
                      from, as a name a reader can act on.
    - ``as_of``     — ISO-8601 instant the underlying fact was true, or None
                      when the source supplies no freshness stamp. None is a
                      declared absence (the adapter says so), never a silent
                      default (§2.3); the renderer marks it unverifiable.
    - ``evidence``  — a deep link to the thing behind it: the run, log,
                      artifact, issue or PR. May be None only when the source
                      has nothing linkable, which the adapter declares.
    - ``cadence_seconds``
                    — how often the source that supplied this fact is RE-READ.
                      Not part of §5.1's four-field contract; it is the
                      qualifier without which `as_of` cannot be interpreted.
                      An `as_of` is a claim about when the underlying fact was
                      last *seen*, and an observer polling every 900s cannot
                      produce one fresher than 900s old however healthy the
                      thing it watches is. Any consumer comparing `as_of`
                      against a declared expectation therefore needs BOTH
                      numbers, or it measures the phase offset between two
                      schedules and calls the result health
                      (alpha-engine-config-I7126).

                      Stamped by the index at ingest from the adapter's own
                      ``AdapterResult.declared_cadence_seconds`` — never by
                      the adapter at each call site, so it cannot drift from
                      the cadence the same result declares to §5.9. ``None``
                      means the source did not say, which is a declared
                      absence and never a licence to assume zero.
    """

    source: str
    as_of: str | None = None
    evidence: str | None = None
    cadence_seconds: float | None = None


@dataclass(frozen=True)
class Entity:
    """One typed thing the console renders a fact about.

    ``id`` is the identifier the source of truth assigned — a component_id,
    run id, cycle id, artifact key, metric name, tracker ref or incident id.
    ``facets`` carries the §2.2 slicing fields (owner, substrate, repo, …);
    ``relations`` are the forward edges this entity declares. The reverse
    edges are derived by the index, never stored twice (§3.3).
    """

    kind: Kind
    id: str
    # console-policy.md §5.1: one of observability-policy.md §8.3's thirteen for
    # anything that resolves to a component state (Component, Run); otherwise
    # the source's own value, verbatim. An Artifact is fresh or stale; an issue
    # is open or closed. Forcing those into a component vocabulary is what
    # produced the `UNKNOWN` fall-through §8.3 forbids by name.
    state: State | str
    provenance: Provenance
    facets: dict[str, str] = field(default_factory=dict)
    # Free-form kind-specific fields (a Run's exit code, a Signal's baseline,
    # an Artifact's schema version). Rendered, never indexed on.
    detail: dict[str, object] = field(default_factory=dict)
    # Set by the index when several claims about this identifier merged (§2.5).
    # Maps a field name to the Provenance of the claim that WON it, so §5.1's
    # `source` stays truthful on a merged row: a row assembled from three
    # sources names three sources, per field, rather than collapsing to
    # whichever adapter happened to run last. Empty on an unmerged entity —
    # `provenance` is then the source of every field.
    field_sources: dict[str, Provenance] = field(default_factory=dict)
    # Claims that LOST a contested field, retained so the disagreement stays
    # visible on the entity page rather than being silently discarded (§2.5).
    # Each is (field name, the value it proposed, its Provenance).
    superseded: tuple[tuple[str, object, Provenance], ...] = ()
    # Field names where two claims of EQUAL rank disagreed and precedence
    # could not settle it. Non-empty means the entity renders DEGRADED with
    # the disagreement named, and it counts toward §9.9.
    conflicts: tuple[str, ...] = ()

    def source_of(self, field_name: str) -> Provenance:
        """Which claim supplied this field (§5.1's `source`, per field).

        Falls back to the entity's own provenance, which is the right answer
        for an unmerged entity: one claim supplied everything.
        """
        return self.field_sources.get(field_name, self.provenance)

    def __post_init__(self) -> None:
        """A Component or Run MUST carry a §8.3 state — no raw value, ever.

        This is the totality invariant made structural: the one place a
        fall-through could re-enter is a component whose adapter reached for a
        string because the enum had no comfortable member. It cannot.
        """
        if self.kind in COMPONENT_STATE_KINDS and not isinstance(self.state, State):
            raise ValueError(
                f"{self.kind.value} {self.id!r} carries state {self.state!r}: a "
                "component state must be one of observability-policy.md §8.3's "
                "thirteen (§5.1). Where the classifier cannot place it, the "
                "answer is State.UNREPORTED — loud, not blank."
            )

    @property
    def state_value(self) -> str:
        """The rendered token, whichever half of §5.1 this row is on."""
        return self.state.value if isinstance(self.state, State) else str(self.state)

    @property
    def route(self) -> str:
        """This entity's addressable URL path — `/<kind>/<id>` (§3.2)."""
        return f"/{self.kind.route}/{self.id}"


@dataclass(frozen=True)
class Edge:
    """A typed relation between two entities (§3.3).

    Edges are declared by the entity that knows them (usually the forward
    direction — a producer names what it produces). The index derives the
    reverse edge so every relation is navigable from both ends; nothing is
    maintained in two places.

    ``rel`` is the forward predicate from ``source`` to ``target``, e.g.
    ``produces``, ``consumed-by``, ``belongs-to``, ``affects``, ``blocks``,
    ``measures``. The reverse predicate is derived by the index.
    """

    source: str  # entity id
    rel: str
    target: str  # entity id


#: The closed set of forward relation predicates and their derived reverses
#: (§3.3). One spelling per relation, shared by adapters, the index and the
#: renderer. Adding a predicate is a model change, not a string at a call site.
RELATIONS: dict[str, str] = {
    "produces": "produced-by",
    "consumed-by": "consumes",
    "belongs-to": "contains",
    "affects": "affected-by",
    "blocks": "blocked-by",
    "measures": "measured-by",
    "runs-on": "hosts",
    # §3.3 — a substrate publishing under a second name, both directions
    # traversable from either the alias's page or the parent's
    # (alpha-engine-config-I8779). Deliberately not symmetric under one
    # spelling: the forward edge names which id is the ALIAS, so a reader on
    # either page can tell the parent from the alias rather than seeing two
    # identical "alias-of" links and having to guess.
    "alias-of": "has-alias",
}
