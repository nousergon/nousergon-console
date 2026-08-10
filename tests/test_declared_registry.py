"""declared-registry adapter tests — fixture YAML files, hermetic.

Covers `nousergon-console#58`: an artifact registry (list-shaped) and an
observation registry (mapping-shaped) are the same DECLARATION source shape —
one YAML document naming many entities of one configured kind — that
`yaml-directory` (one file per Component) does not cover.
"""
from __future__ import annotations

from console.adapters import declared_registry
from console.config import ADAPTERS
from console.index.graph import Index
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


# ---------------------------------------------------------------- shapes ---


def test_list_shaped_registry_becomes_many_artifacts(tmp_path):
    path = _write(tmp_path, "artifacts.yaml", """
- key: s3://fixture-bucket/reports/a.json
  cadence_minutes: 60
- key: s3://fixture-bucket/reports/b.json
  cadence_minutes: 1440
""")
    result = declared_registry.fetch({"path": path, "kind": "artifact", "id_field": "key"})
    assert result.status is AdapterStatus.OK
    ids = {e.id for e in result.entities}
    assert ids == {"s3://fixture-bucket/reports/a.json", "s3://fixture-bucket/reports/b.json"}
    assert all(e.kind is Kind.ARTIFACT for e in result.entities)


def test_mapping_shaped_registry_uses_the_key_as_id_fallback(tmp_path):
    path = _write(tmp_path, "observations.yaml", """
rollout-alpha:
  state: gated-on
  owner: brian
rollout-beta:
  state: always-on
""")
    result = declared_registry.fetch({"path": path, "kind": "decision", "state_field": "state"})
    by_id = {e.id: e for e in result.entities}
    assert set(by_id) == {"rollout-alpha", "rollout-beta"}
    assert all(e.kind is Kind.DECISION for e in result.entities)


def test_explicit_id_field_wins_over_mapping_key(tmp_path):
    path = _write(tmp_path, "observations.yaml", """
ignored-key:
  id: rollout-real-id
  state: gated-off
""")
    result = declared_registry.fetch({"path": path, "kind": "decision", "state_field": "state"})
    assert result.entities[0].id == "rollout-real-id"


# ---------------------------------------------------------------- state ----


def test_state_field_carried_verbatim_for_a_non_component_kind(tmp_path):
    path = _write(tmp_path, "observations.yaml", """
- id: rollout-alpha
  state: gated-off
""")
    result = declared_registry.fetch({"path": path, "kind": "decision", "state_field": "state"})
    # Decision does not resolve to a component state (§5.1's second half): the
    # source's own value renders verbatim, as a raw string.
    assert result.entities[0].state == "gated-off"


def test_default_state_used_when_state_field_absent(tmp_path):
    path = _write(tmp_path, "artifacts.yaml", "- key: s3://fixture-bucket/x.json\n")
    result = declared_registry.fetch({"path": path, "kind": "artifact", "id_field": "key", "default_state": "absent"})
    # No observation adapter claims this key: the lone DECLARATION claim IS
    # the rendered entity, so its base state is what "missing" reads as.
    assert result.entities[0].state == "absent"


def test_component_target_kind_never_carries_a_raw_state(tmp_path):
    path = _write(tmp_path, "components.yaml", "- id: comp-x\n")
    result = declared_registry.fetch({"path": path, "kind": "component", "state_field": "irrelevant", "default_state": "irrelevant"})
    # §2.5 / docs/adapters.md: "a declaration does not supply state" — a
    # Component/Run target ignores state_field/default_state entirely and
    # renders the same UNREPORTED yaml-directory would for an in-service row
    # nothing has observed.
    assert result.entities[0].state is State.UNREPORTED


def test_declared_lifecycle_renders_the_declared_state_for_any_kind(tmp_path):
    path = _write(tmp_path, "artifacts.yaml", """
- key: s3://fixture-bucket/retired.json
  lifecycle: retired
""")
    result = declared_registry.fetch({"path": path, "kind": "artifact", "id_field": "key"})
    assert result.entities[0].state == State.RETIRED.value
    assert isinstance(result.entities[0].state, str)


# ------------------------------------------------------------ facets/detail --


def test_facets_and_detail_and_lineage_edges(tmp_path):
    path = _write(tmp_path, "artifacts.yaml", """
- key: s3://fixture-bucket/x.json
  owner: brian
  produces: []
  consumes: [comp-writer]
  fields:
    row_count:
      value: 903
      render: count
""")
    result = declared_registry.fetch({"path": path, "kind": "artifact", "id_field": "key"})
    ent = result.entities[0]
    assert ent.facets.get("owner") == "brian"
    assert ent.detail["fields"]["row_count"]["value"] == 903
    assert ("comp-writer", "consumed-by", "s3://fixture-bucket/x.json") in {
        (e.source, e.rel, e.target) for e in result.edges
    }


# -------------------------------------------------------------- failures ---


def test_missing_path_is_failed_state_not_exception():
    result = declared_registry.fetch({"path": "/no/such/file.yaml", "kind": "artifact"})
    assert result.status is AdapterStatus.FAILED
    assert "all" in result.unavailable


def test_missing_kind_is_failed(tmp_path):
    path = _write(tmp_path, "x.yaml", "- id: a\n")
    result = declared_registry.fetch({"path": path})
    assert result.status is AdapterStatus.FAILED
    assert "kind" in result.unavailable


def test_unknown_kind_is_failed(tmp_path):
    path = _write(tmp_path, "x.yaml", "- id: a\n")
    result = declared_registry.fetch({"path": path, "kind": "not-a-real-kind"})
    assert result.status is AdapterStatus.FAILED
    assert "kind" in result.unavailable


def test_entries_missing_an_id_are_skipped_and_named(tmp_path):
    path = _write(tmp_path, "x.yaml", "- owner: brian\n- id: real\n")
    result = declared_registry.fetch({"path": path, "kind": "decision"})
    assert [e.id for e in result.entities] == ["real"]
    assert "invalid-entries" in result.unavailable


def test_entries_field_digs_into_a_nested_document(tmp_path):
    path = _write(tmp_path, "x.yaml", """
observations:
  - id: rollout-alpha
    state: gated-on
""")
    result = declared_registry.fetch({"path": path, "kind": "decision", "entries_field": "observations", "state_field": "state"})
    assert [e.id for e in result.entities] == ["rollout-alpha"]


def test_declared_registry_is_registered():
    assert "declared-registry" in ADAPTERS


# ------------------------------------------------------- merge integration --


def test_missing_artifact_computed_from_declaration_alone_when_unobserved(tmp_path):
    """The acceptance case: a registry declares two artifacts, an observation
    source only reports one of them — the un-observed one renders "absent"
    (an exception-list member) purely from the merge, no bespoke code."""
    path = _write(tmp_path, "artifacts.yaml", """
- key: reports/seen.json
  cadence_minutes: 60
- key: reports/never-landed.json
  cadence_minutes: 60
""")
    declared = declared_registry.fetch({
        "path": path, "kind": "artifact", "id_field": "key", "default_state": "absent",
    })

    from console.adapters import object_store

    def lister(bucket, prefix):
        # object-store's own identifier is the listed key verbatim (§2.1) — a
        # matching registry entry must declare the SAME string.
        return [("reports/seen.json", "2026-08-10T00:00:00Z")]

    from datetime import datetime, timezone

    observed = object_store.fetch(
        {"bucket": "fixture-bucket", "prefix": "reports/", "key_pattern": r"reports/.*", "cadence": "1h"},
        lister=lister,
        now=datetime(2026, 8, 10, 0, 30, tzinfo=timezone.utc),
    )

    idx = Index()
    idx.add_result(AdapterResult(name="registry", status=AdapterStatus.OK,
                                  claim_class=ClaimClass.DECLARATION, entities=declared.entities))
    idx.add_result(AdapterResult(name="bucket", status=AdapterStatus.OK,
                                  claim_class=ClaimClass.OBSERVATION, entities=observed.entities))

    assert idx.entity("reports/seen.json").state == "fresh"
    assert idx.entity("reports/never-landed.json").state == "absent"
