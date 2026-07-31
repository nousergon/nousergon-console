"""Shared fixture builders for tests — a tiny synthetic fleet.

Fixtures use synthetic names only (this repo is public; no fleet topology).
They give every test the same small, fully-known graph so reachability,
relations and URL round-trips are asserted against a fleet whose correct
answer is obvious by inspection.
"""
from __future__ import annotations

from console.model.entity import Edge, Entity, Provenance
from console.model.kinds import Kind, State


def prov(source: str = "fixture", as_of: str | None = "2026-07-31T00:00:00Z") -> Provenance:
    return Provenance(source=source, as_of=as_of, evidence=f"fixture://{source}")


def component(cid: str = "comp-alpha", state: State = State.HEALTHY, **facets: str) -> Entity:
    return Entity(kind=Kind.COMPONENT, id=cid, state=state,
                  provenance=prov("registry"), facets=facets)


def artifact(key: str = "s3://fixture-bucket/data/x.json") -> Entity:
    return Entity(kind=Kind.ARTIFACT, id=key, state=State.HEALTHY,
                  provenance=prov("object-store"))


def fixture_graph() -> tuple[list[Entity], list[Edge]]:
    """A three-entity fleet: one component produces one artifact; the artifact
    is consumed by a second component. Enough to assert bidirectional traversal
    and three-path reachability."""
    producer = component("comp-producer")
    consumer = component("comp-consumer")
    art = artifact()
    entities = [producer, consumer, art]
    edges = [
        Edge(source=producer.id, rel="produces", target=art.id),
        Edge(source=consumer.id, rel="consumed-by", target=art.id),
    ]
    return entities, edges
