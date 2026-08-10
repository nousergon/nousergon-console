"""§9.1 population completeness and §9.2 transparency-gap count.

console-policy.md §9.1/§9.2 defer to observability-policy.md §8.4 for the
definition: population completeness is registry rows RENDERED ÷ registry
rows, and separately the UNREGISTERED count; the transparency-gap count is
components in UNREPORTED. Both are computable against a fixture/example
registry now (nousergon-console#16) — the real fleet registry is tracked
separately (alpha-engine-config-I6115) — and §11's N/A-NOT-IMPL carve-out
does NOT cover either number, so an unreadable registry must render an
honest "not computable" shape instead of the reserved placeholder token.
"""
from __future__ import annotations

from console.config import build_index
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State


def _write_registry(dirpath, components):
    import os
    import yaml

    for cid, extra in components.items():
        row = {"component_id": cid, "lifecycle": "in-service", "owner": "x", **extra}
        with open(os.path.join(dirpath, f"{cid}.yaml"), "w") as fh:
            yaml.safe_dump(row, fh)


def test_population_completeness_over_a_healthy_registry_is_1(tmp_path):
    reg = tmp_path / "registry.d"
    reg.mkdir()
    _write_registry(str(reg), {"comp-one": {}, "comp-two": {}})
    config = {"registry": {"adapter": "yaml-directory", "path": str(reg),
                           "id_field": "component_id"}}
    index = build_index(config)
    result = index.population_completeness()
    assert result["computable"] is True
    assert result["ratio"] == 1.0
    assert result["rendered"] == 2
    assert result["of"] == 2


def test_population_completeness_names_its_denominator_not_total_entities():
    """The defect this replaces: denominator was `len(entities)` — every kind,
    including entities a registry never declared. Here a non-component entity
    must not inflate the registry-row denominator."""
    idx = Index()
    idx.declare_registry("registry")
    idx.add_result(AdapterResult(
        name="registry", status=AdapterStatus.OK, claim_class=ClaimClass.DECLARATION,
        entities=(Entity(kind=Kind.COMPONENT, id="comp-one", state=State.UNREPORTED,
                         provenance=Provenance("registry")),),
    ))
    idx.record_registry_rows("registry", count=1, ok=True)
    idx.add_result(AdapterResult(
        name="checks", status=AdapterStatus.OK, claim_class=ClaimClass.OBSERVATION,
        entities=(Entity(kind=Kind.ARTIFACT, id="s3://b/k.json", state="fresh",
                         provenance=Provenance("checks")),),
    ))
    result = idx.population_completeness()
    assert result["of"] == 1  # ONE registry row, not two entities


def test_population_completeness_uncomputable_with_no_registry():
    idx = Index()
    result = idx.population_completeness()
    assert result["computable"] is False
    assert "N/A-NOT-IMPL" not in str(result)  # the reserved token is forbidden here (§11)
    assert result["reason"]


def test_population_completeness_uncomputable_when_a_registry_is_unreadable(tmp_path):
    reg = tmp_path / "registry.d"
    reg.mkdir()
    _write_registry(str(reg), {"comp-one": {}})
    config = {"registries": [
        {"name": "primary", "adapter": "yaml-directory", "path": str(reg),
         "id_field": "component_id"},
        {"name": "secondary", "adapter": "yaml-directory",
         "path": str(tmp_path / "does-not-exist"), "id_field": "component_id"},
    ]}
    index = build_index(config)
    result = index.population_completeness()
    # A second, unreadable registry must not let the ratio silently report 1.0
    # over just the registry that happened to work (§5.3 — no aggregate over
    # incomplete input).
    assert result["computable"] is False
    assert "secondary" in result["reason"]


def test_population_completeness_reports_unregistered_separately(tmp_path):
    """§8.4: "the first says the surface shows what it knows about; the second
    says how much it did not know about" — two numbers, never blended."""
    reg = tmp_path / "registry.d"
    reg.mkdir()
    _write_registry(str(reg), {"comp-one": {}})
    config = {"registry": {"adapter": "yaml-directory", "path": str(reg),
                           "id_field": "component_id"},
              "adapters": [{"name": "units", "kind": "local-units", "enabled": True,
                            "config": {"unit_prefixes": []}}]}
    index = build_index(config)
    # Inject a discovery claim for a component the registry never declared.
    index.add_result(AdapterResult(
        name="discovery", status=AdapterStatus.OK, claim_class=ClaimClass.DISCOVERY,
        entities=(Entity(kind=Kind.COMPONENT, id="comp-wild", state=State.UNREPORTED,
                         provenance=Provenance("discovery")),),
    ))
    result = index.population_completeness()
    assert result["unregistered"] == 1


def test_transparency_gap_denominator_is_component_population_not_all_entities():
    idx = Index()
    idx.add_result(AdapterResult(
        name="registry", status=AdapterStatus.OK, claim_class=ClaimClass.DECLARATION,
        entities=(
            Entity(kind=Kind.COMPONENT, id="comp-reported", state=State.HEALTHY,
                  provenance=Provenance("registry")),
            Entity(kind=Kind.COMPONENT, id="comp-silent", state=State.UNREPORTED,
                  provenance=Provenance("registry")),
        ),
    ))
    idx.add_result(AdapterResult(
        name="checks", status=AdapterStatus.OK, claim_class=ClaimClass.OBSERVATION,
        entities=(Entity(kind=Kind.ARTIFACT, id="s3://b/k.json", state="fresh",
                         provenance=Provenance("checks")),),
    ))
    result = idx.transparency_gap()
    assert result == {"count": 1, "of": 2}  # 2 components, not 3 entities


def test_transparency_gap_is_zero_when_nothing_is_unreported():
    idx = Index()
    idx.add_result(AdapterResult(
        name="registry", status=AdapterStatus.OK, claim_class=ClaimClass.DECLARATION,
        entities=(Entity(kind=Kind.COMPONENT, id="comp-one", state=State.HEALTHY,
                         provenance=Provenance("registry")),),
    ))
    assert idx.transparency_gap() == {"count": 0, "of": 1}
