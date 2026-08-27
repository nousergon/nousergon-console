"""`s3-records` DRIVER tests — one descriptor-declared document, no live
bucket (`nousergon-console#98`, blocking `alpha-engine-config-I7477`).

Fixture body mirrors the shape `console-policy.md`'s report-card v2
(`report_card.json`) actually carries — `tiles.*.components`, the grouped
fan-out `console/records_shape.py::project` already covers for the sibling
`s3-records` **adapter** (`tests/test_s3_records.py`). This file exercises the
DRIVER direction: one document, read via `context["document_reader"]`, same
grammar.
"""
from __future__ import annotations

from datetime import datetime, timezone

from console.config import build_index
from console.drivers import DRIVERS, s3_records
from console.model.descriptor import Binding
from console.model.kinds import Kind, State

NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)

REPORT_CARD = {
    "as_of": "2026-08-16T04:00:00+00:00",
    "tiles": {
        "research": {
            "components": [
                {
                    "name": "signal-quality", "value": 0.62, "ci_low": 0.55,
                    "ci_high": 0.69, "n_samples": 240, "target": 0.6,
                    "red_line": 0.4, "status": "GREEN",
                    "status_reason": "above target for 4 consecutive weeks",
                },
                {
                    "name": "coverage", "value": 0.31, "ci_low": 0.2,
                    "ci_high": 0.42, "n_samples": 240, "target": 0.5,
                    "red_line": 0.2, "status": "WATCH",
                    "status_reason": "below target, above red line",
                },
            ],
        },
        "execution": {
            "components": [
                {
                    "name": "fill-quality", "value": 0.9, "ci_low": 0.85,
                    "ci_high": 0.95, "n_samples": 500, "target": 0.85,
                    "red_line": 0.6, "status": "GREEN",
                    "status_reason": "steady",
                },
            ],
        },
    },
}


def _binding(**spec) -> Binding:
    return Binding(component_id="crucible-report-card", kind="metrics",
                    driver="s3-records", spec=spec)


def _report_card_spec(**extra):
    return {
        "key": "s3://research-bucket/evaluator/2026-08-16/report_card.json",
        "kind": "signal",
        "records_path": "tiles.*.components",
        "group_field": "tile",
        "id_template": "{tile}:{name}",
        "state_field": "status",
        "fields": {
            "value": {"path": "value"},
            "ci_low": {"path": "ci_low"},
            "ci_high": {"path": "ci_high"},
            "n_samples": {"path": "n_samples", "render": "count"},
            "target": {"path": "target"},
            "red_line": {"path": "red_line"},
            "reason": {"path": "status_reason", "render": "text"},
        },
        "cadence_minutes": 1440,
        **extra,
    }


def _ctx(**extra):
    return {"document_reader": lambda key: REPORT_CARD, "now": NOW, **extra}


def test_is_registered():
    assert DRIVERS["s3-records"] is s3_records
    assert s3_records.name == "s3-records"


def test_grouped_fan_out_yields_one_signal_per_tile_component():
    binding = _binding(**_report_card_spec())
    result = s3_records.read(binding, _ctx())
    assert result.ok
    ids = {e.id for e in result.entities}
    assert ids == {
        "research:signal-quality", "research:coverage", "execution:fill-quality",
    }
    assert all(e.kind is Kind.SIGNAL for e in result.entities)


def test_declared_fields_and_state_field_render_verbatim_for_a_non_component_kind():
    """Signal is not a COMPONENT_STATE_KIND (§5.1's second half): the source's
    own state value renders verbatim, not mapped through the thirteen."""
    binding = _binding(**_report_card_spec())
    result = s3_records.read(binding, _ctx())
    by_id = {e.id: e for e in result.entities}
    coverage = by_id["research:coverage"]
    assert coverage.state == "WATCH"
    fields = coverage.detail["fields"]
    assert fields["value"]["value"] == 0.31
    assert fields["target"]["value"] == 0.5
    assert fields["reason"]["value"] == "below target, above red line"


def test_group_field_is_injected_and_reachable_by_id_template():
    binding = _binding(**_report_card_spec())
    result = s3_records.read(binding, _ctx())
    ids = {e.id for e in result.entities}
    assert "execution:fill-quality" in ids  # tile name from the dict key, not the body


def test_body_level_as_of_reaches_every_fanned_out_record():
    binding = _binding(**_report_card_spec(as_of_field="as_of"))
    result = s3_records.read(binding, _ctx())
    assert all(e.provenance.as_of == "2026-08-16T04:00:00+00:00" for e in result.entities)


def test_no_document_reader_is_a_named_failure():
    binding = _binding(**_report_card_spec())
    result = s3_records.read(binding, {"now": NOW})
    assert not result.ok
    assert "reader" in result.unavailable


def test_declared_and_never_written_is_a_named_finding():
    def reader(key):
        raise FileNotFoundError(key)
    binding = _binding(**_report_card_spec())
    result = s3_records.read(binding, _ctx(document_reader=reader))
    assert not result.ok
    assert "never written" in result.error


def test_missing_kind_fails_the_binding():
    spec = _report_card_spec()
    del spec["kind"]
    binding = _binding(**spec)
    result = s3_records.read(binding, _ctx())
    assert not result.ok
    assert "kind" in result.unavailable


def test_missing_key_fails_the_binding():
    binding = _binding(kind="signal")
    result = s3_records.read(binding, _ctx())
    assert not result.ok
    assert "key" in result.unavailable


def test_a_record_with_no_resolvable_id_is_partial_not_a_crash():
    spec = _report_card_spec(id_template="{tile}:{missing_field}")
    binding = _binding(**spec)
    result = s3_records.read(binding, _ctx())
    assert result.ok  # the binding as a whole still reports
    assert result.entities == ()
    assert "record" in result.unavailable


def test_component_state_kind_maps_through_the_closed_vocabulary():
    """Bind the same document as `component` instead of `signal` — proves the
    driver goes through the shared §5.1 component-state branch too, not just
    the raw-verbatim one."""
    spec = _report_card_spec(kind="component", id_template="{tile}-{name}",
                             state_map={"GREEN": "HEALTHY", "WATCH": "DEGRADED"})
    binding = _binding(**spec)
    result = s3_records.read(binding, _ctx())
    by_id = {e.id: e for e in result.entities}
    assert by_id["research-signal-quality"].state is State.HEALTHY
    assert by_id["research-coverage"].state is State.DEGRADED


def test_produces_edge_names_the_document_as_the_component_produces_it():
    binding = _binding(**_report_card_spec())
    result = s3_records.read(binding, _ctx())
    assert any(
        e.rel == "produces" and e.target == binding.spec["key"]
        and e.source == "crucible-report-card"
        for e in result.edges
    )


def test_produces_edge_also_reaches_every_fanned_out_record():
    """alpha-engine-config-I8768: the document-level `produces` edge above
    named only `key` — none of the three fanned-out Signal ids equal the
    document key, so they carried zero inbound edges regardless of the
    binding declaring them. Each record's own id is this driver's own read
    (§2.3), so the fix is naming it too."""
    binding = _binding(**_report_card_spec())
    result = s3_records.read(binding, _ctx())
    rels = {(e.source, e.rel, e.target) for e in result.edges}
    assert ("crucible-report-card", "produces", "research:signal-quality") in rels
    assert ("crucible-report-card", "produces", "research:coverage") in rels
    assert ("crucible-report-card", "produces", "execution:fill-quality") in rels
    # The document-level edge is still there, exactly once.
    assert len([e for e in result.edges if e.target == binding.spec["key"]]) == 1


def test_the_driver_wires_through_a_descriptor_end_to_end(tmp_path):
    """The whole point: a component in a place no console config points at
    onboards through its own descriptor (§2.6)."""
    import yaml

    registry_dir = tmp_path / "registry.d"
    registry_dir.mkdir()
    (registry_dir / "crucible-report-card.yaml").write_text(yaml.safe_dump({
        "component_id": "crucible-report-card",
        "owner": "brian",
        "lifecycle": "in-service",
        "metrics": {"driver": "s3-records", **_report_card_spec()},
    }))
    config = {
        "registry": {"adapter": "yaml-directory", "path": str(registry_dir),
                     "id_field": "component_id"},
        "_driver_context": _ctx(),
    }
    index = build_index(config)
    signal = index.entity("research:signal-quality")
    assert signal is not None and signal.kind is Kind.SIGNAL
    assert signal.detail["fields"]["value"]["value"] == 0.62
