"""Index graph tests — bidirectional relations, namespace collision, §9.3."""
from __future__ import annotations

import pytest

from console.index.graph import Index, NamespaceCollision
from console.model.envelope import AdapterResult, AdapterStatus
from console.model.kinds import Kind, State
from tests.fixtures import component, fixture_graph, prov
from console.model.entity import Entity


def _index_with(entities, edges, status=AdapterStatus.OK) -> Index:
    idx = Index()
    idx.add_result(AdapterResult(name="fixture", status=status,
                                 entities=tuple(entities), edges=tuple(edges)))
    return idx


def test_reverse_edge_is_derived_not_stored():
    entities, edges = fixture_graph()
    idx = _index_with(entities, edges)
    art = next(e for e in entities if e.kind is Kind.ARTIFACT)
    inbound = [e for e in idx.related(art.id)]
    # Producer's forward `produces` derives a reverse `produced-by` on the artifact.
    preds = {(e.rel) for e in inbound}
    assert "produced-by" in preds
    assert "consumes" in preds


def test_relation_traversable_from_both_ends():
    entities, edges = fixture_graph()
    idx = _index_with(entities, edges)
    producer = next(e for e in entities if e.id == "comp-producer")
    art = next(e for e in entities if e.kind is Kind.ARTIFACT)
    # Forward: producer → artifact. Reverse: artifact → both components.
    assert any(e.target == art.id for e in idx.related(producer.id))
    # Reverse edges are keyed at the artifact: `consumes` points back at the consumer.
    consumers = {e.target for e in idx.related(art.id) if e.rel == "consumes"}
    assert "comp-consumer" in consumers


def test_namespace_collision_fails_loudly():
    a = component("dup-id")
    b = component("dup-id")
    with pytest.raises(NamespaceCollision):
        _index_with([a, b], [])


def test_failed_adapter_marks_entities_unreported_not_dropped():
    entities, edges = fixture_graph()
    idx = _index_with(entities, edges, status=AdapterStatus.FAILED)
    assert len(idx.all()) == 3  # nothing dropped
    assert all(e.state is State.UNREPORTED for e in idx.all())


def test_reachability_ratio_names_denominator():
    entities, edges = fixture_graph()
    idx = _index_with(entities, edges)
    r = idx.reachability()
    assert r["total"] == 3
    # Only the artifact has an inbound edge in this fixture.
    assert r["reachable_all_three"] == 1
    assert r["ratio"] == round(1 / 3, 4)


def test_reachability_empty_index_is_null_not_zero():
    assert Index().reachability()["ratio"] is None
