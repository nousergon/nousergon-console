"""`dated-snapshot` adapter tests — recorded key lists + documents, no live bucket."""
from __future__ import annotations

from datetime import datetime, timezone

from console.adapters import dated_snapshot
from console.model.envelope import AdapterStatus
from console.model.kinds import Kind, State

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


def _lister(objects):
    def lister(bucket, prefix):
        return objects
    return lister


# ------------------------------------------------------------- signal kind --

def _signal_cfg(**over):
    return {
        "bucket": "b", "prefix": "producer_leaderboard/",
        "key_pattern": r"producer_leaderboard/(?P<date>[^/]+)\.json",
        "id_template": "producer-leaderboard:{date}",
        "kind": "signal",
        "fields": {
            "champion_arm": {"path": "champion", "render": "text"},
            "challenger_score": {"path": "challenger.score", "unit": "value", "render": "value"},
        },
        "cadence": "7d",
        **over,
    }


def _reader(bodies):
    return bodies.get


def test_signal_kind_carries_source_value_verbatim_not_a_component_state():
    objects = [("producer_leaderboard/2026-08-08.json", "2026-08-08T00:00:00Z")]
    bodies = {"producer_leaderboard/2026-08-08.json": {"champion": "arm-a", "challenger": {"score": 0.42}, "status": "LIVE"}}
    result = dated_snapshot.fetch(
        {**_signal_cfg(), "state_field": "status"},
        lister=_lister(objects), document_reader=_reader(bodies), now=NOW,
    )
    assert result.status is AdapterStatus.OK
    ent = result.entities[0]
    assert ent.kind is Kind.SIGNAL
    assert ent.state == "live"
    assert ent.id == "producer-leaderboard:2026-08-08"
    assert ent.detail["fields"]["champion_arm"]["value"] == "arm-a"
    assert ent.detail["fields"]["challenger_score"]["value"] == 0.42


def test_signal_without_state_field_defaults_to_reporting():
    objects = [("producer_leaderboard/2026-08-08.json", "2026-08-08T00:00:00Z")]
    bodies = {"producer_leaderboard/2026-08-08.json": {"champion": "arm-a", "challenger": {"score": 0.42}}}
    result = dated_snapshot.fetch(_signal_cfg(), lister=_lister(objects), document_reader=_reader(bodies), now=NOW)
    assert result.entities[0].state == "reporting"


# ---------------------------------------------------------------- run kind --

def _run_cfg(**over):
    return {
        "bucket": "b", "prefix": "predictor/metrics/",
        "key_pattern": r"predictor/metrics/training_summary_(?P<date>[^/]+)\.json",
        "id_template": "predictor-training:{date}",
        "kind": "run",
        "state_field": "promoted",
        "state_map": {"True": "HEALTHY", "False": "DEGRADED"},
        "fields": {
            "ic_gate_passed": {"path": "ic_gate_passed", "render": "text"},
        },
        "cadence": "7d",
        **over,
    }


def test_run_kind_resolves_through_state_map_to_a_closed_state():
    objects = [("predictor/metrics/training_summary_2026-08-08.json", "2026-08-08T00:00:00Z")]
    bodies = {"predictor/metrics/training_summary_2026-08-08.json": {"promoted": True, "ic_gate_passed": True}}
    result = dated_snapshot.fetch(_run_cfg(), lister=_lister(objects), document_reader=_reader(bodies), now=NOW)
    ent = result.entities[0]
    assert ent.kind is Kind.RUN
    assert ent.state is State.HEALTHY  # a Run resolves to §8.3 (COMPONENT_STATE_KINDS)


def test_run_kind_unmapped_raw_value_is_degraded_never_a_fabricated_green():
    objects = [("predictor/metrics/training_summary_2026-08-08.json", "2026-08-08T00:00:00Z")]
    bodies = {"predictor/metrics/training_summary_2026-08-08.json": {"promoted": "unknown-status"}}
    result = dated_snapshot.fetch(_run_cfg(), lister=_lister(objects), document_reader=_reader(bodies), now=NOW)
    ent = result.entities[0]
    assert ent.state is State.DEGRADED
    assert ent.detail["raw_state"] == "unknown-status"


# ------------------------------------------------------------------ shared --

def test_unmatched_keys_skipped():
    objects = [("other/path/file.json", "2026-08-08T00:00:00Z")]
    result = dated_snapshot.fetch(_signal_cfg(), lister=_lister(objects), document_reader=_reader({}), now=NOW)
    assert result.entities == ()


def test_a_broken_individual_snapshot_is_excluded_not_a_source_wide_failure():
    objects = [
        ("producer_leaderboard/2026-08-01.json", "2026-08-01T00:00:00Z"),  # unreadable
        ("producer_leaderboard/2026-08-08.json", "2026-08-08T00:00:00Z"),  # fine
    ]
    def reader(key):
        if "08-01" in key:
            raise RuntimeError("truncated write")
        return {"champion": "arm-a", "challenger": {"score": 0.1}}
    result = dated_snapshot.fetch(_signal_cfg(), lister=_lister(objects), document_reader=reader, now=NOW)
    assert result.status is AdapterStatus.OK
    assert len(result.entities) == 1
    assert result.entities[0].id == "producer-leaderboard:2026-08-08"


def test_lister_failure_is_failed_not_empty():
    def boom(b, p):
        raise RuntimeError("bucket unreachable")
    result = dated_snapshot.fetch(_signal_cfg(), lister=boom, document_reader=_reader({}), now=NOW)
    assert result.status is AdapterStatus.FAILED


def test_missing_required_config_is_a_named_failure():
    cfg = {"bucket": "b", "key_pattern": "x"}  # no kind, no id_template
    result = dated_snapshot.fetch(cfg, lister=_lister([]), document_reader=_reader({}), now=NOW)
    assert result.status is AdapterStatus.FAILED
