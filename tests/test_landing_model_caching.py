"""alpha-engine-config-I10615: the landing view's model is computed ONCE per
`Index`, never once per request.

`render.html.landing_page` and `render.json.payload`'s landing view used to
each recompute `landing_exceptions`, `index.conflicts()`,
`index.reachability()`, `index.registry_coverage()`, `index.decision_queue()`,
`index.population_completeness()`, `index.transparency_gap()`,
`render.json.numbers` and the milestone predicate independently, per request
— all pure functions of one already-built, immutable `Index`. These tests
assert the fix: both representations read `Index.landing_model()`, which
computes exactly once per `Index` instance and is never shared across
instances.
"""
from __future__ import annotations

import console.index.landing as landing_module
from console.index.graph import Index
from console.render import html as render_html
from console.render import json as render_json
from console.server.router import resolve


def test_landing_model_computed_once_per_index_across_both_representations(monkeypatch):
    calls = {"n": 0}
    real_build = landing_module.build

    def counting_build(index):
        calls["n"] += 1
        return real_build(index)

    monkeypatch.setattr(landing_module, "build", counting_build)

    index = Index()
    index.finalize()

    # Two renders of each representation against the SAME index.
    render_html.landing_page(index)
    render_json.payload(index, resolve("/"))
    render_html.landing_page(index)
    render_json.payload(index, resolve("/"))

    assert calls["n"] == 1, (
        "landing model recomputed on a later render of an unchanged index — "
        f"expected exactly 1 build, got {calls['n']}"
    )


def test_landing_model_is_the_same_object_across_repeated_reads():
    index = Index()
    index.finalize()

    first = index.landing_model()
    second = index.landing_model()

    assert first is second


def test_landing_model_is_not_shared_across_index_instances():
    idx1 = Index()
    idx2 = Index()
    idx1.finalize()
    idx2.finalize()

    model1 = idx1.landing_model()
    model2 = idx2.landing_model()

    # Distinct instances get distinct models — no cross-index leakage.
    assert model1 is not model2
    # A brand-new Index never inherits another instance's cached model: the
    # cache lives on the instance, dropped with it, never on the class.
    assert Index()._landing_model is None


def test_a_new_claim_invalidates_the_cached_landing_model():
    """§5.6: a console-held fact must never be able to drift from its source.
    Adding a claim after the model was read must not leave a stale model
    attached to an index that now carries a claim the model never saw.
    """
    from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
    from console.model.entity import Entity
    from console.model.kinds import Kind, State
    from console.model.entity import Provenance

    index = Index()
    index.finalize()
    first = index.landing_model()

    prov = Provenance(source="fixture", as_of="2026-09-13T00:00:00Z",
                       evidence="fixture://test")
    index.add_result(AdapterResult(
        name="f", status=AdapterStatus.OK, claim_class=ClaimClass.DECLARATION,
        entities=(Entity(kind=Kind.COMPONENT, id="comp-new", state=State.HEALTHY,
                          provenance=prov),),
    ))

    second = index.landing_model()
    assert second is not first
    # The new component is reflected in the freshly rebuilt graph and in the
    # model's own completeness count, not just quietly absent from a stale one.
    assert index.entity("comp-new") is not None
    assert second.completeness["of"] == 1
