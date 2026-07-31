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

from .kinds import Kind, State


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
    """

    source: str
    as_of: str | None = None
    evidence: str | None = None


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
    state: State
    provenance: Provenance
    facets: dict[str, str] = field(default_factory=dict)
    # Free-form kind-specific fields (a Run's exit code, a Signal's baseline,
    # an Artifact's schema version). Rendered, never indexed on.
    detail: dict[str, object] = field(default_factory=dict)

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
}
