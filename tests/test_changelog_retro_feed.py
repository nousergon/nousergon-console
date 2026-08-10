"""changelog-retro-feed adapter tests — one recorded aggregate document, no
live bucket (nousergon-console#60)."""
from __future__ import annotations

from datetime import datetime, timezone

from console.adapters import changelog_retro_feed
from console.config import ADAPTERS
from console.model.envelope import AdapterStatus
from console.model.kinds import Kind

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
BUCKET = "fixture-research-bucket"
KEY = "changelog/retro_candidates.json"

FEED = {
    "generated_at": "2026-08-10T06:00:00Z",
    "window_start": "2026-07-11",
    "window_end": "2026-08-10",
    "window_days": 30,
    "incident_group_count": 2,
    "incident_total": 5,
    "ready_for_retro_count": 1,
    "incident_groups": [
        {
            "subsystem": "preopen-sf",
            "summary": "preopen SF wedged on CFN stack",
            "severity": "critical",
            "count": 3,
            "latest_ts": "2026-08-09T10:00:00Z",
            "has_writeup": True,
        },
        {
            "subsystem": "scanner",
            "summary": "universe gate mismatch",
            "severity": "high",
            "count": 2,
            "latest_ts": "2026-08-08T09:00:00Z",
            "has_writeup": False,
        },
    ],
    "ready_for_retro": [
        {
            "subsystem": "preopen-sf",
            "summary": "preopen SF wedged on CFN stack",
            "severity": "critical",
            "root_cause_category": "wedged-stack",
            "resolution_type": "code-fix",
            "resolution_notes": "Root cause: the CFN stack entered UPDATE_ROLLBACK_FAILED " * 3,
            "git_refs": [{"repo": "nous-ergon-ops", "pr_number": 42}],
            "ts_utc": "2026-08-09T18:00:00Z",
        },
    ],
}


def _reader(bucket, key):
    assert bucket == BUCKET
    assert key == KEY
    return dict(FEED)


def _cfg(**extra):
    return {"bucket": BUCKET, "key": KEY, "cadence": "1d", **extra}


def _by_id(result):
    return {e.id: e for e in result.entities}


def test_groups_become_incidents_keyed_by_subsystem_and_summary():
    result = changelog_retro_feed.fetch(_cfg(), reader=_reader, now=NOW)
    assert result.status is AdapterStatus.OK
    ids = {e.id for e in result.entities}
    assert ids == {
        "preopen-sf|preopen SF wedged on CFN stack",
        "scanner|universe gate mismatch",
    }
    assert all(e.kind is Kind.INCIDENT for e in result.entities)


def test_has_writeup_maps_to_ready_for_retro_state():
    result = changelog_retro_feed.fetch(_cfg(), reader=_reader, now=NOW)
    by_id = _by_id(result)
    assert by_id["preopen-sf|preopen SF wedged on CFN stack"].state == "ready-for-retro"


def test_no_writeup_maps_to_needs_triage_state():
    result = changelog_retro_feed.fetch(_cfg(), reader=_reader, now=NOW)
    by_id = _by_id(result)
    assert by_id["scanner|universe gate mismatch"].state == "needs-triage"


def test_ready_for_retro_narrative_merged_by_join_key():
    result = changelog_retro_feed.fetch(_cfg(), reader=_reader, now=NOW)
    by_id = _by_id(result)
    resolution = by_id["preopen-sf|preopen SF wedged on CFN stack"].detail["resolution"]
    assert resolution["root_cause_category"] == "wedged-stack"
    assert resolution["resolution_type"] == "code-fix"


def test_group_without_writeup_carries_no_resolution_detail():
    result = changelog_retro_feed.fetch(_cfg(), reader=_reader, now=NOW)
    by_id = _by_id(result)
    assert "resolution" not in by_id["scanner|universe gate mismatch"].detail


def test_group_detail_carries_count_and_severity():
    result = changelog_retro_feed.fetch(_cfg(), reader=_reader, now=NOW)
    by_id = _by_id(result)
    group = by_id["scanner|universe gate mismatch"]
    assert group.detail["count"] == 2
    assert group.detail["severity"] == "high"


def test_four_field_row_contract():
    result = changelog_retro_feed.fetch(_cfg(), reader=_reader, now=NOW)
    ev = next(iter(result.entities))
    assert ev.provenance.source == f"s3://{BUCKET}/{KEY}"
    assert ev.provenance.evidence == f"s3://{BUCKET}/{KEY}"
    assert ev.provenance.as_of is not None


def test_declared_cadence_parsed_from_config():
    result = changelog_retro_feed.fetch(_cfg(), reader=_reader, now=NOW)
    assert result.declared_cadence_seconds == 86400.0


def test_empty_feed_is_ok_with_no_entities():
    def empty_reader(b, k):
        return {"generated_at": "2026-08-10T06:00:00Z", "incident_groups": [], "ready_for_retro": []}

    result = changelog_retro_feed.fetch(_cfg(), reader=empty_reader, now=NOW)
    assert result.status is AdapterStatus.OK
    assert result.entities == ()


def test_reader_failure_is_failed_not_empty():
    def boom(b, k):
        raise RuntimeError("object not found")

    result = changelog_retro_feed.fetch(_cfg(), reader=boom, now=NOW)
    assert result.status is AdapterStatus.FAILED
    assert "source" in result.unavailable
    assert result.entities == ()


def test_missing_key_is_failed():
    result = changelog_retro_feed.fetch({"bucket": BUCKET}, reader=_reader, now=NOW)
    assert result.status is AdapterStatus.FAILED


def test_no_reader_is_failed(monkeypatch):
    monkeypatch.setattr(changelog_retro_feed, "_default_reader", lambda: None)
    result = changelog_retro_feed.fetch(_cfg(), now=NOW)
    assert result.status is AdapterStatus.FAILED
    assert "reader" in result.unavailable


def test_changelog_retro_feed_is_registered():
    assert "changelog-retro-feed" in ADAPTERS
