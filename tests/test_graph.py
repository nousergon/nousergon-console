"""Index graph tests — bidirectional relations, namespace collision, §9.3."""
from __future__ import annotations

import pytest

from console.index.graph import Index, NamespaceCollision
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from tests.fixtures import component, fixture_graph, prov
from console.model.entity import Edge, Entity


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
    assert r["search_reachable"] == 3
    assert r["structure_reachable"] == 3
    # Only the artifact has an inbound edge in this fixture.
    assert r["relation_reachable"] == 1
    assert r["reachable_all_three"] == 1
    assert r["ratio"] == round(1 / 3, 4)


def test_reachability_measures_each_path_independently(monkeypatch):
    """§9.3 executes every path; a broken resolver lowers only its count."""
    entities, edges = fixture_graph()
    idx = _index_with(entities, edges)

    from console.index import reachability

    baseline = idx.reachability()
    monkeypatch.setattr(reachability, "search", lambda index, query: [])
    broken_search = idx.reachability()

    assert broken_search["search_reachable"] == 0
    assert broken_search["structure_reachable"] == baseline["structure_reachable"]
    assert broken_search["relation_reachable"] == baseline["relation_reachable"]


def test_reachability_empty_index_is_null_not_zero():
    assert Index().reachability()["ratio"] is None


def test_unregistered_reconciler_leaves_run_state_alone():
    """alpha-engine-config-I8766: `_reconcile`'s UNREGISTERED/ABSENT branches
    are about a COMPONENT's declaration, not a RUN's own outcome. A run of an
    unregistered component (`checks_envelope`'s `<check_id>@<ran_at>` ids)
    never matches any registry row by construction, so treating RUN like
    COMPONENT here rewrote every one of it to UNREGISTERED once per run
    (154 live rows on 2026-08-27) instead of flagging the component once.
    """
    idx = Index()
    # A successful declaration pass registers one component, leaving a
    # second component undeclared, and contributes a RUN entity that no
    # registry could ever match — the adapter's own assigned state must
    # survive `_reconcile` unchanged.
    idx.add_result(AdapterResult(
        name="registry", status=AdapterStatus.OK,
        claim_class=ClaimClass.DECLARATION,
        entities=(Entity(kind=Kind.COMPONENT, id="comp-declared",
                         state=State.UNREPORTED, provenance=prov("registry")),),
    ))
    idx.add_result(AdapterResult(
        name="checks_envelope", status=AdapterStatus.OK,
        claim_class=ClaimClass.OBSERVATION,
        entities=(
            Entity(kind=Kind.COMPONENT, id="comp-undeclared",
                   state=State.HEALTHY, provenance=prov("checks_envelope")),
            Entity(kind=Kind.RUN, id="check-x@2026-08-27T00:00:00Z",
                   state=State.HEALTHY, provenance=prov("checks_envelope")),
            Entity(kind=Kind.RUN, id="check-y@2026-08-27T00:05:00Z",
                   state=State.FAILED, provenance=prov("checks_envelope")),
        ),
    ))
    idx.finalize()

    run_ok = idx.entity("check-x@2026-08-27T00:00:00Z")
    run_failed = idx.entity("check-y@2026-08-27T00:05:00Z")
    undeclared_component = idx.entity("comp-undeclared")

    # Runs keep the state their adapter assigned...
    assert run_ok.state is State.HEALTHY
    assert run_failed.state is State.FAILED
    # ...while an undeclared COMPONENT is still flagged.
    assert undeclared_component.state is State.UNREGISTERED

    completeness = idx.population_completeness()
    assert completeness["unregistered"] == 1
    assert completeness["unregistered_ids"] == ["comp-undeclared"]


# ------------------------ alias ids (alpha-engine-config-I8779) -------------
#
# An alias is the same substrate process publishing under a second name, not
# a second registry row: `population_completeness().of` must count the parent
# once, and an alias's OBSERVATION claim must merge onto its own declaration
# rather than reading UNREGISTERED.


def test_an_alias_declaration_does_not_inflate_the_denominator():
    idx = Index()
    idx.add_result(AdapterResult(
        name="registry", status=AdapterStatus.OK,
        claim_class=ClaimClass.DECLARATION,
        entities=(
            Entity(kind=Kind.COMPONENT, id="comp-parent",
                   state=State.UNREPORTED, provenance=prov("registry")),
            Entity(kind=Kind.COMPONENT, id="comp-alias",
                   state=State.UNREPORTED, provenance=prov("registry"),
                   detail={"alias_of": "comp-parent"}),
        ),
        edges=(Edge(source="comp-alias", rel="alias-of", target="comp-parent"),),
    ))
    completeness = idx.population_completeness()
    assert completeness["of"] == 1
    assert completeness["rendered"] == 1


def test_alias_of_is_traversable_from_both_ends():
    idx = Index()
    idx.add_result(AdapterResult(
        name="registry", status=AdapterStatus.OK,
        claim_class=ClaimClass.DECLARATION,
        entities=(
            Entity(kind=Kind.COMPONENT, id="comp-parent",
                   state=State.UNREPORTED, provenance=prov("registry")),
            Entity(kind=Kind.COMPONENT, id="comp-alias",
                   state=State.UNREPORTED, provenance=prov("registry"),
                   detail={"alias_of": "comp-parent"}),
        ),
        edges=(Edge(source="comp-alias", rel="alias-of", target="comp-parent"),),
    ))
    idx.finalize()
    assert any(
        e.rel == "alias-of" and e.target == "comp-parent"
        for e in idx.related("comp-alias")
    )
    assert any(
        e.rel == "has-alias" and e.target == "comp-alias"
        for e in idx.related("comp-parent")
    )


def test_an_aliased_id_observed_but_never_declared_elsewhere_is_not_unregistered():
    """The acceptance case: the substrate reports under the alias id, and the
    registry's declaration (minted by `yaml-directory` under the SAME alias
    id) is what keeps it out of `unregistered`."""
    idx = Index()
    idx.add_result(AdapterResult(
        name="registry", status=AdapterStatus.OK,
        claim_class=ClaimClass.DECLARATION,
        entities=(
            Entity(kind=Kind.COMPONENT, id="comp-parent",
                   state=State.UNREPORTED, provenance=prov("registry")),
            Entity(kind=Kind.COMPONENT, id="comp-alias",
                   state=State.UNREPORTED, provenance=prov("registry"),
                   detail={"alias_of": "comp-parent"}),
        ),
    ))
    idx.add_result(AdapterResult(
        name="checks_envelope", status=AdapterStatus.OK,
        claim_class=ClaimClass.OBSERVATION,
        entities=(
            Entity(kind=Kind.COMPONENT, id="comp-alias",
                   state=State.HEALTHY, provenance=prov("checks_envelope")),
        ),
    ))
    idx.finalize()
    alias = idx.entity("comp-alias")
    assert alias.state is State.HEALTHY
    completeness = idx.population_completeness()
    assert completeness["unregistered"] == 0
    assert completeness["of"] == 1


# ------------------------ alias state inheritance (alpha-engine-config-I8973) -
#
# #8779 mints a DECLARATION for every alias; #8973 covers what happens to its
# STATE. Two live shapes, both legitimate: a rename alias (nothing ever
# reports under the old name — it must not render UNREPORTED on top of a live
# parent) and the #8779 shape (the alias IS the emitted name, the parent may
# be the one that never reports).


def test_a_silent_alias_inherits_the_parents_state_and_as_of():
    """Rename-alias shape: `alias_ids: [old-name]` on a row now emitted under
    the new name (I6838). The alias carries only its own DECLARATION — no
    OBSERVATION/DISCOVERY ever arrives under the old name — and must inherit
    the parent's merged state/as-of rather than reading UNREPORTED. 23 of
    these, live, inflated `transparency_gap` 14 -> 37 before this existed."""
    idx = Index()
    idx.add_result(AdapterResult(
        name="registry", status=AdapterStatus.OK,
        claim_class=ClaimClass.DECLARATION,
        entities=(
            Entity(kind=Kind.COMPONENT, id="comp-parent",
                   state=State.UNREPORTED, provenance=prov("registry")),
            Entity(kind=Kind.COMPONENT, id="comp-old-name",
                   state=State.UNREPORTED, provenance=prov("registry"),
                   detail={"alias_of": "comp-parent"}),
        ),
        edges=(Edge(source="comp-old-name", rel="alias-of", target="comp-parent"),),
    ))
    idx.add_result(AdapterResult(
        name="checks_envelope", status=AdapterStatus.OK,
        claim_class=ClaimClass.OBSERVATION,
        entities=(
            Entity(kind=Kind.COMPONENT, id="comp-parent", state=State.HEALTHY,
                   provenance=prov("checks_envelope", as_of="2026-08-27T23:00:00Z")),
        ),
    ))
    idx.finalize()

    alias = idx.entity("comp-old-name")
    assert alias.state is State.HEALTHY
    assert alias.detail["alias_state"] == "inherited"
    assert alias.provenance.as_of == "2026-08-27T23:00:00Z"

    # Excluded from BOTH the count and the denominator: it is a pointer to
    # the parent, already counted, not a second silent component.
    assert idx.transparency_gap() == {"count": 0, "of": 1}


def test_an_observed_alias_keeps_its_own_state_and_the_unobserved_parent_inherits():
    """#8779 shape: the substrate reports under the alias id, not the
    declared parent id. The alias keeps its own state (unchanged from
    PR115); the parent, carrying no report of its own, inherits FROM it —
    the observation belongs to the running process, not to whichever of its
    two names happened to receive the registry row."""
    idx = Index()
    idx.add_result(AdapterResult(
        name="registry", status=AdapterStatus.OK,
        claim_class=ClaimClass.DECLARATION,
        entities=(
            Entity(kind=Kind.COMPONENT, id="comp-parent",
                   state=State.UNREPORTED, provenance=prov("registry")),
            Entity(kind=Kind.COMPONENT, id="comp-emitted-name",
                   state=State.UNREPORTED, provenance=prov("registry"),
                   detail={"alias_of": "comp-parent"}),
        ),
        edges=(Edge(source="comp-emitted-name", rel="alias-of", target="comp-parent"),),
    ))
    idx.add_result(AdapterResult(
        name="checks_envelope", status=AdapterStatus.OK,
        claim_class=ClaimClass.OBSERVATION,
        entities=(
            Entity(kind=Kind.COMPONENT, id="comp-emitted-name", state=State.HEALTHY,
                   provenance=prov("checks_envelope", as_of="2026-08-27T23:00:00Z")),
        ),
    ))
    idx.finalize()

    alias = idx.entity("comp-emitted-name")
    parent = idx.entity("comp-parent")
    assert alias.state is State.HEALTHY
    assert "alias_state" not in alias.detail

    assert parent.state is State.HEALTHY
    assert parent.detail["alias_state"] == "inherited"
    assert parent.provenance.as_of == "2026-08-27T23:00:00Z"

    # The alias DID receive its own report, so it stays counted; the parent
    # is no longer UNREPORTED either way.
    assert idx.transparency_gap() == {"count": 0, "of": 2}
