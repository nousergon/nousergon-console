"""`document-fields` driver tests — recorded documents, no live bucket."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from console.config import build_index
from console.drivers import document_fields
from console.model.descriptor import Binding
from console.model.kinds import State

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


def _binding(**spec) -> Binding:
    return Binding(component_id="predictor", kind="metrics",
                    driver="document-fields", spec=spec)


def test_two_documents_combine_into_one_entitys_fields():
    """The `manifest.json` / `latest.json` split (nousergon-console#56's own
    gotcha): training fields and rolling fields come from two different
    files and must both land on the one Component entity."""
    docs = {
        "s3://b/latest.json": {"model_version": "v42", "hit_rate_30d_rolling": 0.62},
        "s3://b/manifest.json": {"promoted": True, "last_trained": "2026-08-08"},
    }
    binding = _binding(documents=[
        {"key": "s3://b/latest.json", "fields": {
            "model_version": {"path": "model_version", "render": "text"},
            "hit_rate": {"path": "hit_rate_30d_rolling", "unit": "ratio", "baseline": 0.55, "render": "ratio"},
        }},
        {"key": "s3://b/manifest.json", "fields": {
            "promoted": {"path": "promoted", "render": "text"},
            "last_trained": {"path": "last_trained", "render": "text"},
        }},
    ])
    result = document_fields.read(binding, {"document_reader": docs.get, "now": NOW})
    assert result.ok
    ent = result.entities[0]
    fields = ent.detail["fields"]
    assert fields["model_version"]["value"] == "v42"
    assert fields["hit_rate"]["baseline"] == 0.55
    assert fields["promoted"]["value"] is True
    assert fields["last_trained"]["value"] == "2026-08-08"


def test_dotted_path_extracts_nested_fields():
    docs = {"s3://b/prod_health.json": {"l1_components": {"momentum": 0.03}}}
    binding = _binding(documents=[
        {"key": "s3://b/prod_health.json", "fields": {
            "momentum_ic": {"path": "l1_components.momentum", "render": "value", "unit": "ic"},
        }},
    ])
    result = document_fields.read(binding, {"document_reader": docs.get, "now": NOW})
    assert result.entities[0].detail["fields"]["momentum_ic"]["value"] == 0.03


def test_missing_nested_path_is_none_not_a_crash():
    docs = {"s3://b/x.json": {"a": {}}}
    binding = _binding(documents=[
        {"key": "s3://b/x.json", "fields": {"missing": {"path": "a.b.c"}}},
    ])
    result = document_fields.read(binding, {"document_reader": docs.get, "now": NOW})
    assert result.entities[0].detail["fields"]["missing"]["value"] is None


def test_no_field_map_dumps_the_whole_document_opaque_never_dropped():
    """§5.8: an undeclared field renders opaque and is counted, never dropped."""
    docs = {"s3://b/x.json": {"n_predictions_today": 14}}
    binding = _binding(documents=[{"key": "s3://b/x.json"}])
    result = document_fields.read(binding, {"document_reader": docs.get, "now": NOW})
    assert result.entities[0].detail["fields"]["n_predictions_today"] == 14


def test_no_reader_is_a_named_failure_not_an_empty_entity():
    binding = _binding(documents=[{"key": "s3://b/x.json"}])
    result = document_fields.read(binding, {"now": NOW})
    assert not result.ok
    assert result.unavailable == ("reader",)


def test_a_declared_but_never_written_document_is_a_named_finding():
    def reader(key):
        raise FileNotFoundError(key)
    binding = _binding(documents=[{"key": "s3://b/none.json"}])
    result = document_fields.read(binding, {"document_reader": reader, "now": NOW})
    assert not result.ok
    assert "never written" in result.error


def test_a_half_written_document_says_so_rather_than_guessing():
    binding = _binding(documents=[{"key": "s3://b/x.json"}])
    result = document_fields.read(binding, {"document_reader": lambda k: "{not json", "now": NOW})
    assert not result.ok
    assert "did not parse" in result.error


def test_state_missed_when_past_cadence():
    docs = {"s3://b/x.json": {"a": 1}}
    binding = _binding(documents=[{"key": "s3://b/x.json"}], cadence_minutes=60)
    stat = lambda k: "2026-08-08T12:00:00+00:00"  # ~2 days stale vs 60m cadence
    result = document_fields.read(binding, {"document_reader": docs.get, "object_stat": stat, "now": NOW})
    assert result.entities[0].state is State.MISSED


def test_state_healthy_when_fresh():
    docs = {"s3://b/x.json": {"a": 1}}
    binding = _binding(documents=[{"key": "s3://b/x.json"}], cadence_minutes=60)
    stat = lambda k: "2026-08-10T11:50:00+00:00"  # 10 minutes old
    result = document_fields.read(binding, {"document_reader": docs.get, "object_stat": stat, "now": NOW})
    assert result.entities[0].state is State.HEALTHY


def test_no_documents_declared_is_a_failed_binding():
    binding = _binding()
    result = document_fields.read(binding, {"now": NOW})
    assert not result.ok
    assert result.unavailable == ("binding",)


# ------------------------------------------------- full-stack via build_index --

def _config(tmp_path, docs):
    registry = tmp_path / "registry.d"
    registry.mkdir(parents=True)
    (registry / "predictor.yaml").write_text(
        "component_id: predictor\n"
        "metrics:\n"
        "  driver: document-fields\n"
        "  documents:\n"
        "    - key: s3://b/latest.json\n"
        "      fields:\n"
        "        model_version: {path: model_version, render: text}\n"
        "    - key: s3://b/manifest.json\n"
        "      fields:\n"
        "        promoted: {path: promoted, render: text}\n"
    )
    return {
        "registry": {"adapter": "yaml-directory", "path": str(registry)},
        "_driver_context": {"document_reader": docs.get},
    }


def test_end_to_end_through_build_index(tmp_path):
    docs = {
        "s3://b/latest.json": {"model_version": "v42"},
        "s3://b/manifest.json": {"promoted": True},
    }
    index = build_index(_config(tmp_path, docs))
    ent = index.entity("predictor")
    assert ent.detail["fields"]["model_version"]["value"] == "v42"
    assert ent.detail["fields"]["promoted"]["value"] is True
    # Two documents, one entity, no spurious DEGRADED from a false conflict.
    assert ent.conflicts == ()


# --- calendar symbols (alpha-engine-config-I12854) ---------------------------

def _weekdays(date_str: str) -> bool:
    return datetime.fromisoformat(date_str).date().weekday() < 5


MONDAY_MORNING = datetime(2026, 8, 17, 9, 25, tzinfo=timezone.utc)


def _read_symbolic(stamp, now, cadence="weekday_sf"):
    docs = {"s3://b/x.json": {"a": 1}}
    binding = _binding(documents=[{"key": "s3://b/x.json"}], cadence=cadence)
    return document_fields.read(binding, {
        "document_reader": docs.get, "object_stat": lambda k: stamp,
        "now": now, "trading_day_checker": _weekdays})


def test_a_weekday_producer_is_healthy_on_monday_from_fridays_write():
    """A literal 1440 would read Friday's write MISSED all weekend."""
    result = _read_symbolic("2026-08-14T12:31:00+00:00", MONDAY_MORNING)
    assert result.entities[0].state is State.HEALTHY
    assert result.cadence_seconds is not None


def test_a_weekday_producer_silent_for_a_week_is_missed():
    result = _read_symbolic("2026-08-07T12:31:00+00:00", MONDAY_MORNING)
    assert result.entities[0].state is State.MISSED


def test_a_literal_cadence_still_wins_over_a_symbol():
    docs = {"s3://b/x.json": {"a": 1}}
    binding = _binding(documents=[{"key": "s3://b/x.json"}], cadence="weekday_sf",
                       cadence_minutes=60)
    result = document_fields.read(binding, {
        "document_reader": docs.get, "object_stat": lambda k: "2026-08-14T12:31:00+00:00",
        "now": MONDAY_MORNING, "trading_day_checker": _weekdays})
    assert result.cadence_seconds == 3600.0
    assert result.entities[0].state is State.MISSED


def test_event_driven_yields_no_cadence():
    result = _read_symbolic("2026-08-01T00:00:00+00:00", MONDAY_MORNING, cadence="event_driven")
    assert result.cadence_seconds is None
