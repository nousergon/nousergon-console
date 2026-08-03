"""Index graph tests — bidirectional relations, namespace collision, §9.3."""
from __future__ import annotations

import pytest

from console.index.graph import Index, NamespaceCollision
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
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


def test_same_kind_duplicate_merges_rather_than_raising():
    """§2.5: two sources describing one thing is the NORMAL case.

    This test previously asserted the opposite. The index raised on the second
    claim for one id, which made the fleet's own intended configuration — a
    registry declaring a component plus telemetry observing it — crash the
    surface, and made `UNREGISTERED`/`ABSENT` uncomputable by construction.
    """
    idx = Index()
    idx.add_result(AdapterResult(
        name="registry", status=AdapterStatus.OK,
        claim_class=ClaimClass.DECLARATION,
        entities=(Entity(kind=Kind.COMPONENT, id="dup-id",
                         state=State.UNREPORTED, provenance=prov("registry")),),
    ))
    idx.add_result(AdapterResult(
        name="telemetry", status=AdapterStatus.OK,
        claim_class=ClaimClass.OBSERVATION,
        entities=(Entity(kind=Kind.COMPONENT, id="dup-id",
                         state=State.HEALTHY, provenance=prov("telemetry")),),
    ))
    assert len(idx.all()) == 1
    merged = idx.entity("dup-id")
    # The declaration ranks first, so its UNREPORTED wins the state — the
    # registry says it exists and nothing observed it into health yet... except
    # telemetry DID, and that is exactly what precedence must not swallow.
    # A declaration supplies existence and lifecycle; it does not supply state,
    # so an in-service row's UNREPORTED must lose to a real observation.
    assert merged.state is State.HEALTHY
    assert merged.source_of("state").source == "telemetry"


def test_different_kind_duplicate_still_fails_loudly():
    """§3.6 is unchanged: two DIFFERENT things sharing a name is a collision,
    and `kind` disagreement is the discriminator no precedence rule can fix."""
    idx = Index()
    idx.add_result(AdapterResult(
        name="a", status=AdapterStatus.OK,
        entities=(Entity(kind=Kind.COMPONENT, id="dup-id", state=State.HEALTHY,
                         provenance=prov("a")),)))
    idx.add_result(AdapterResult(
        name="b", status=AdapterStatus.OK,
        entities=(Entity(kind=Kind.ARTIFACT, id="dup-id", state="fresh",
                         provenance=prov("b")),)))
    with pytest.raises(NamespaceCollision):
        idx.all()


def test_failed_adapter_marks_entities_unreported_not_dropped():
    entities, edges = fixture_graph()
    idx = _index_with(entities, edges, status=AdapterStatus.FAILED)
    assert len(idx.all()) == 3  # nothing dropped
    # Components carry §8.3's UNREPORTED; non-component kinds carry the raw
    # value (§5.1's second half) — an artifact is not "unreported", its SOURCE
    # is, and the two read differently to whoever is looking.
    for e in idx.all():
        if e.kind in (Kind.COMPONENT, Kind.RUN):
            assert e.state is State.UNREPORTED
        else:
            assert e.state == "unreported-by-source"


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
