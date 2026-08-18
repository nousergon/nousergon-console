"""changelog-events adapter tests — recorded fixtures, no live bucket.

Covers both configured instances of this adapter (nousergon-console#60):
the raw event-lake (`changelog/entries/`, `state_field: severity`) and the
vocab-quarantine sibling (`changelog/quarantine/`, `state_literal: quarantined`).
"""
from __future__ import annotations

from datetime import datetime, timezone

from console.adapters import changelog_events
from console.config import ADAPTERS
from console.model.envelope import AdapterStatus
from console.model.kinds import Kind

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
BUCKET = "fixture-research-bucket"

ENTRIES_PREFIX = "changelog/entries/"
QUARANTINE_PREFIX = "changelog/quarantine/"

KEY_PATTERN = r".*/(?P<day>\d{4}-\d{2}-\d{2})/(?P<event_id>[^/]+)\.json$"

CRITICAL_EVENT = {
    "ts_utc": "2026-08-09T10:00:00Z",
    "event_type": "alert",
    "severity": "critical",
    "subsystem": "preopen-sf",
    "root_cause_category": "wedged-stack",
    "summary": "preopen SF wedged",
    "actor": "sf-watch",
    "source": "changelog-incident-mirror",
    "event_id": "evt-critical-1",
    "flow_doctor": {"error_signature": "CFN_WEDGED", "dedup_count": 3},
}
INFO_EVENT = {
    "ts_utc": "2026-08-09T11:00:00Z",
    "event_type": "notification",
    "severity": "informational",
    "subsystem": "groom",
    "summary": "groom cycle completed",
    "actor": "groom-loop",
    "source": "flow-doctor",
    "event_id": "evt-info-1",
}
QUARANTINE_EVENT = {
    "ts_utc": "2026-08-09T12:00:00Z",
    "event_type": "alert",
    "severity": "high",
    "subsystem": "scanner",
    "summary": "universe gate mismatch",
    "actor": "scanner",
    "source": "changelog-cloudwatch-mirror",
    "event_id": "evt-quarantined-1",
    "validation_errors": ["subsystem 'scaner' not in vocab.yaml"],
}

ENTRY_BODIES = {
    f"{ENTRIES_PREFIX}2026-08-09/evt-critical-1.json": CRITICAL_EVENT,
    f"{ENTRIES_PREFIX}2026-08-09/evt-info-1.json": INFO_EVENT,
}
QUARANTINE_BODIES = {
    f"{QUARANTINE_PREFIX}2026-08-09/evt-quarantined-1.json": QUARANTINE_EVENT,
}


def _lister(bodies):
    def lister(bucket, prefix):
        assert bucket == BUCKET
        return [(k, v.get("ts_utc")) for k, v in bodies.items()]
    return lister


def _reader(bodies):
    def reader(bucket, key):
        assert bucket == BUCKET
        return dict(bodies[key])
    return reader


def _entries_cfg(**extra):
    return {
        "bucket": BUCKET,
        "prefix": ENTRIES_PREFIX,
        "key_pattern": KEY_PATTERN,
        "id_template": "{event_id}",
        "state_field": "severity",
        **extra,
    }


def _quarantine_cfg(**extra):
    return {
        "bucket": BUCKET,
        "prefix": QUARANTINE_PREFIX,
        "key_pattern": KEY_PATTERN,
        "id_template": "{day}/{event_id}",
        "state_literal": "quarantined",
        **extra,
    }


def _by_id(result):
    return {e.id: e for e in result.entities}


def test_entries_become_incidents_keyed_by_event_id():
    result = changelog_events.fetch(
        _entries_cfg(), lister=_lister(ENTRY_BODIES), reader=_reader(ENTRY_BODIES), now=NOW,
    )
    assert result.status is AdapterStatus.OK
    incidents = {e.id for e in result.entities if e.kind is Kind.INCIDENT}
    assert incidents == {"evt-critical-1", "evt-info-1"}


def test_state_field_reads_severity_verbatim_lowercased():
    result = changelog_events.fetch(
        _entries_cfg(), lister=_lister(ENTRY_BODIES), reader=_reader(ENTRY_BODIES), now=NOW,
    )
    by_id = _by_id(result)
    assert by_id["evt-critical-1"].state == "critical"
    assert by_id["evt-info-1"].state == "informational"


def test_incident_is_not_forced_into_component_vocabulary():
    # §5.1 second half — an Incident carries the source's own value, a plain
    # str, never observability-policy.md §8.3's thirteen-state enum.
    result = changelog_events.fetch(
        _entries_cfg(), lister=_lister(ENTRY_BODIES), reader=_reader(ENTRY_BODIES), now=NOW,
    )
    critical = _by_id(result)["evt-critical-1"]
    assert isinstance(critical.state, str)


def test_four_field_row_contract():
    result = changelog_events.fetch(
        _entries_cfg(), lister=_lister(ENTRY_BODIES), reader=_reader(ENTRY_BODIES), now=NOW,
    )
    ev = _by_id(result)["evt-critical-1"]
    assert ev.provenance.source.startswith("s3://")
    assert ev.provenance.as_of == "2026-08-09T10:00:00Z"
    assert ev.provenance.evidence == f"s3://{BUCKET}/{ENTRIES_PREFIX}2026-08-09/evt-critical-1.json"


def test_detail_carries_schema_fields_including_nested_flow_doctor():
    result = changelog_events.fetch(
        _entries_cfg(), lister=_lister(ENTRY_BODIES), reader=_reader(ENTRY_BODIES), now=NOW,
    )
    ev = _by_id(result)["evt-critical-1"]
    assert ev.detail["subsystem"] == "preopen-sf"
    assert ev.detail["error_signature"] == "CFN_WEDGED"
    assert ev.detail["dedup_count"] == 3


def test_producer_edge_derived_from_schema_source_field():
    result = changelog_events.fetch(
        _entries_cfg(), lister=_lister(ENTRY_BODIES), reader=_reader(ENTRY_BODIES), now=NOW,
    )
    rels = {(e.source, e.rel, e.target) for e in result.edges}
    assert ("changelog-incident-mirror", "produces", "evt-critical-1") in rels
    assert ("flow-doctor", "produces", "evt-info-1") in rels


def test_quarantine_state_is_literal_regardless_of_own_severity():
    result = changelog_events.fetch(
        _quarantine_cfg(), lister=_lister(QUARANTINE_BODIES), reader=_reader(QUARANTINE_BODIES), now=NOW,
    )
    incident = next(iter(result.entities))
    assert incident.state == "quarantined"


def test_quarantine_identifier_composes_day_and_event_id():
    result = changelog_events.fetch(
        _quarantine_cfg(), lister=_lister(QUARANTINE_BODIES), reader=_reader(QUARANTINE_BODIES), now=NOW,
    )
    incident = next(iter(result.entities))
    assert incident.id == "2026-08-09/evt-quarantined-1"


def test_quarantine_carries_validation_errors():
    result = changelog_events.fetch(
        _quarantine_cfg(), lister=_lister(QUARANTINE_BODIES), reader=_reader(QUARANTINE_BODIES), now=NOW,
    )
    incident = next(iter(result.entities))
    assert incident.detail["validation_errors"] == ["subsystem 'scaner' not in vocab.yaml"]


def test_entries_never_carry_validation_errors_key():
    result = changelog_events.fetch(
        _entries_cfg(), lister=_lister(ENTRY_BODIES), reader=_reader(ENTRY_BODIES), now=NOW,
    )
    for ev in result.entities:
        assert "validation_errors" not in ev.detail


def test_unreadable_body_is_unreadable_state_not_dropped():
    def bad_reader(b, k):
        raise RuntimeError("access denied")

    result = changelog_events.fetch(
        _entries_cfg(),
        lister=lambda b, p: [(f"{ENTRIES_PREFIX}2026-08-09/evt-bad.json", "2026-08-09T00:00:00Z")],
        reader=bad_reader,
        now=NOW,
    )
    assert result.status is AdapterStatus.OK
    by_id = _by_id(result)
    assert by_id["evt-bad"].state == "unreadable"
    assert "body" in result.unavailable


def test_lister_failure_is_failed_not_empty():
    def boom(b, p):
        raise RuntimeError("bucket unreachable")

    result = changelog_events.fetch(
        _entries_cfg(), lister=boom, reader=_reader(ENTRY_BODIES), now=NOW,
    )
    assert result.status is AdapterStatus.FAILED
    assert "source" in result.unavailable
    assert result.entities == ()


def test_missing_bucket_is_failed():
    result = changelog_events.fetch(
        {"prefix": ENTRIES_PREFIX}, lister=_lister(ENTRY_BODIES), reader=_reader(ENTRY_BODIES), now=NOW,
    )
    assert result.status is AdapterStatus.FAILED


def test_no_lister_or_reader_is_failed(monkeypatch):
    monkeypatch.setattr(changelog_events, "_default_s3", lambda: (None, None))
    result = changelog_events.fetch(_entries_cfg(), now=NOW)
    assert result.status is AdapterStatus.FAILED
    assert "lister" in result.unavailable
    assert "reader" in result.unavailable


def test_unmatched_keys_skipped():
    result = changelog_events.fetch(
        _entries_cfg(),
        lister=lambda b, p: [("other/path/file.txt", "2026-08-09T00:00:00Z")],
        reader=_reader(ENTRY_BODIES),
        now=NOW,
    )
    assert result.entities == ()


def test_no_state_field_or_literal_is_unspecified():
    result = changelog_events.fetch(
        {"bucket": BUCKET, "prefix": ENTRIES_PREFIX, "key_pattern": KEY_PATTERN,
         "id_template": "{event_id}"},
        lister=_lister(ENTRY_BODIES), reader=_reader(ENTRY_BODIES), now=NOW,
    )
    by_id = _by_id(result)
    assert by_id["evt-critical-1"].state == "unspecified"


def test_changelog_events_is_registered():
    assert "changelog-events" in ADAPTERS
