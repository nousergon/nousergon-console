"""object-store adapter tests — recorded key lists, no live bucket."""
from __future__ import annotations

from datetime import datetime, timezone

from console.adapters import object_store
from console.model.envelope import AdapterStatus
from console.model.kinds import Kind, State

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
PATTERN = r"ops/checks/(?P<component_id>[^/]+)/latest.json"


def _lister(bucket, prefix):
    return [
        ("ops/checks/comp-alpha/latest.json", "2026-07-31T11:30:00Z"),  # fresh
        ("ops/checks/comp-beta/latest.json", "2026-07-30T00:00:00Z"),   # stale
    ]


def _cfg():
    return {
        "bucket": "fixture-bucket",
        "prefix": "ops/checks/",
        "key_pattern": PATTERN,
        "cadence": "1h",
        "staleness_factor": 1.5,
    }


def test_keys_become_artifacts_keyed_by_key():
    result = object_store.fetch(_cfg(), lister=_lister, now=NOW)
    assert result.status is AdapterStatus.OK
    ids = {e.id for e in result.entities}
    assert "ops/checks/comp-alpha/latest.json" in ids
    assert all(e.kind is Kind.ARTIFACT for e in result.entities)


def test_fresh_vs_stale_rendered_from_cadence():
    result = object_store.fetch(_cfg(), lister=_lister, now=NOW)
    by_id = {e.id: e for e in result.entities}
    assert by_id["ops/checks/comp-alpha/latest.json"].state is State.HEALTHY
    assert by_id["ops/checks/comp-beta/latest.json"].state is State.STALE


def test_component_edge_derived_from_named_group():
    result = object_store.fetch(_cfg(), lister=_lister, now=NOW)
    rels = {(e.source, e.rel, e.target) for e in result.edges}
    assert ("comp-alpha", "produces", "ops/checks/comp-alpha/latest.json") in rels


def test_no_stamp_is_unknown_not_healthy():
    def lister(b, p):
        return [("ops/checks/comp-x/latest.json", None)]
    result = object_store.fetch(_cfg(), lister=lister, now=NOW)
    assert result.entities[0].state is State.UNKNOWN


def test_lister_failure_is_failed_not_empty():
    def boom(b, p):
        raise RuntimeError("bucket unreachable")
    result = object_store.fetch(_cfg(), lister=boom, now=NOW)
    assert result.status is AdapterStatus.FAILED


def test_unmatched_keys_skipped():
    def lister(b, p):
        return [("other/path/file.txt", "2026-07-31T11:00:00Z")]
    result = object_store.fetch(_cfg(), lister=lister, now=NOW)
    assert result.entities == ()
