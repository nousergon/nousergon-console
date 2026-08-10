"""yaml-directory adapter tests — fixture registry, hermetic (no live source)."""
from __future__ import annotations

import os

from console.adapters import yaml_directory
from console.model.envelope import AdapterStatus
from console.model.kinds import Kind, State

EXAMPLE_REGISTRY = os.path.join(os.path.dirname(__file__), "..", "example", "registry.d")


def test_reads_one_component_per_file():
    result = yaml_directory.fetch({"path": EXAMPLE_REGISTRY, "id_field": "component_id"})
    assert result.status is AdapterStatus.OK
    ids = {e.id for e in result.entities}
    # comp-gamma (nousergon-console#56) demonstrates a `document-fields`
    # metrics binding — two deliberately split source documents combining
    # onto one component, alongside comp-alpha/comp-beta's plain rows.
    assert ids == {"comp-alpha", "comp-beta", "comp-gamma"}
    assert all(e.kind is Kind.COMPONENT for e in result.entities)


def test_facets_mapped_from_identity_fields():
    result = yaml_directory.fetch({"path": EXAMPLE_REGISTRY, "id_field": "component_id"})
    alpha = next(e for e in result.entities if e.id == "comp-alpha")
    assert alpha.facets.get("substrate") == "lambda"
    assert alpha.facets.get("repo") == "example-repo"


def test_missing_directory_is_failed_state_not_exception():
    result = yaml_directory.fetch({"path": "/no/such/dir", "id_field": "component_id"})
    assert result.status is AdapterStatus.FAILED
    assert "all" in result.unavailable


def test_declared_lifecycle_renders_the_declared_state(tmp_path):
    (tmp_path / "old.yaml").write_text(
        "component_id: comp-old\nlifecycle: retired\nowner: x\n"
    )
    result = yaml_directory.fetch({"path": str(tmp_path), "id_field": "component_id"})
    ent = result.entities[0]
    # §8.3: DISABLED/DEPRECATED/RETIRED are DECLARED in the registry and
    # nothing else may produce them. ABSENT is a different finding entirely —
    # the registry expects it and the substrate lacks it.
    assert ent.state is State.RETIRED


def test_registry_row_is_declared_not_measured(tmp_path):
    # A registry declaration has no telemetry: state is UNKNOWN, as_of is None
    # (declared absence, §2.3), never a guessed freshness.
    (tmp_path / "c.yaml").write_text("component_id: comp-x\nlifecycle: in-service\n")
    result = yaml_directory.fetch({"path": str(tmp_path), "id_field": "component_id"})
    ent = result.entities[0]
    # An in-service row with no observation is UNREPORTED — "registered, in
    # service, emitting nothing", which IS the transparency-gap count. It is
    # never UNKNOWN: §8.3 forbids that escape hatch by name.
    assert ent.state is State.UNREPORTED
    assert ent.provenance.as_of is None
