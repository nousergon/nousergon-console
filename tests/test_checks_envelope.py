"""checks-envelope adapter tests — recorded envelopes, no live bucket.

Fixtures use synthetic check ids only (this repo is public; no fleet topology).
Envelope shape matches the fleet check-result contract (schema_version 1).
"""
from __future__ import annotations

from datetime import datetime, timezone

from console.adapters import checks_envelope
from console.config import ADAPTERS
from console.model.envelope import AdapterStatus
from console.model.kinds import Kind, State

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
BUCKET = "fixture-bucket"
PREFIX = "ops/checks/"

OK_BODY = {
    "schema_version": 1,
    "check_id": "comp-alpha",
    "label": "Alpha check",
    "ran_at": "2026-07-31T11:30:00+00:00",
    "status": "ok",
    "summary": "all clear",
    "cadence_minutes": 60,
    "deep_link": "https://example.test/alpha",
    "findings": [],
}
ATTN_BODY = {
    "schema_version": 1,
    "check_id": "comp-beta",
    "label": "Beta check",
    "ran_at": "2026-07-31T11:00:00+00:00",
    "status": "attention",
    "summary": "one finding",
    "cadence_minutes": 60,
    "deep_link": None,
    "findings": [{"key": "x", "detail": "y"}],
}
ERR_BODY = {
    "schema_version": 1,
    "check_id": "comp-gamma",
    "label": "Gamma check",
    "ran_at": "2026-07-31T11:45:00+00:00",
    "status": "error",
    "summary": "broken",
    "cadence_minutes": 60,
    "findings": [],
}
STALE_OK_BODY = {
    # status ok, but ran_at far older than cadence → STALE wins
    "schema_version": 1,
    "check_id": "comp-stale",
    "label": "Stale check",
    "ran_at": "2026-07-30T00:00:00+00:00",
    "status": "ok",
    "summary": "last write was fine",
    "cadence_minutes": 60,
    "findings": [],
}

BODIES = {
    f"{PREFIX}comp-alpha/latest.json": OK_BODY,
    f"{PREFIX}comp-beta/latest.json": ATTN_BODY,
    f"{PREFIX}comp-gamma/latest.json": ERR_BODY,
    f"{PREFIX}comp-stale/latest.json": STALE_OK_BODY,
}


def _lister(bucket, prefix):
    assert bucket == BUCKET
    return [(k, v.get("ran_at")) for k, v in BODIES.items()]


def _reader(bucket, key):
    assert bucket == BUCKET
    return dict(BODIES[key])


def _cfg(**extra):
    return {
        "bucket": BUCKET,
        "prefix": PREFIX,
        "key_pattern": r"ops/checks/(?P<component_id>[^/]+)/latest\.json",
        "staleness_factor": 1.5,
        **extra,
    }


def _by_id(result):
    return {e.id: e for e in result.entities}


def test_envelopes_become_components_keyed_by_check_id():
    result = checks_envelope.fetch(
        _cfg(), lister=_lister, reader=_reader, now=NOW,
    )
    assert result.status is AdapterStatus.OK
    components = {e.id for e in result.entities if e.kind is Kind.COMPONENT}
    assert components == {"comp-alpha", "comp-beta", "comp-gamma", "comp-stale"}


def test_status_maps_onto_closed_vocabulary():
    result = checks_envelope.fetch(
        _cfg(), lister=_lister, reader=_reader, now=NOW,
    )
    by_id = _by_id(result)
    assert by_id["comp-alpha"].state is State.HEALTHY
    assert by_id["comp-beta"].state is State.DEGRADED
    assert by_id["comp-gamma"].state is State.FAILING


def test_stale_beats_last_ok_status():
    """A dying check's last write is almost always ok — ran_at + cadence mark
    it STALE when publishing stops (fleet_check_result contract)."""
    result = checks_envelope.fetch(
        _cfg(), lister=_lister, reader=_reader, now=NOW,
    )
    assert _by_id(result)["comp-stale"].state is State.STALE


def test_four_field_row_contract_on_component():
    result = checks_envelope.fetch(
        _cfg(), lister=_lister, reader=_reader, now=NOW,
    )
    alpha = _by_id(result)["comp-alpha"]
    assert alpha.provenance.source.startswith("s3://")
    assert alpha.provenance.as_of == "2026-07-31T11:30:00+00:00"
    assert alpha.provenance.evidence == "https://example.test/alpha"
    assert alpha.state is State.HEALTHY


def test_check_id_used_verbatim_never_minted():
    # Path segment and body check_id agree; the body wins when both present,
    # and nothing invents a third identifier (§3.6).
    def reader(b, k):
        body = dict(OK_BODY)
        body["check_id"] = "comp-alpha"  # verbatim
        return body

    result = checks_envelope.fetch(
        _cfg(), lister=lambda b, p: [(f"{PREFIX}comp-alpha/latest.json", None)],
        reader=reader, now=NOW,
    )
    components = [e for e in result.entities if e.kind is Kind.COMPONENT]
    assert [c.id for c in components] == ["comp-alpha"]


def test_artifact_and_run_emitted_with_edges():
    result = checks_envelope.fetch(
        _cfg(), lister=_lister, reader=_reader, now=NOW,
    )
    arts = {e.id for e in result.entities if e.kind is Kind.ARTIFACT}
    runs = [e for e in result.entities if e.kind is Kind.RUN]
    assert f"{PREFIX}comp-alpha/latest.json" in arts
    assert any(r.id.startswith("comp-alpha@") for r in runs)
    rels = {(e.source, e.rel, e.target) for e in result.edges}
    assert ("comp-alpha", "produces", f"{PREFIX}comp-alpha/latest.json") in rels
    assert any(s.startswith("comp-alpha@") and rel == "belongs-to" and t == "comp-alpha"
               for s, rel, t in rels)


def test_unreadable_body_is_unreported_not_dropped():
    def bad_reader(b, k):
        raise RuntimeError("access denied")

    result = checks_envelope.fetch(
        _cfg(),
        lister=lambda b, p: [(f"{PREFIX}comp-x/latest.json", "2026-07-31T11:00:00Z")],
        reader=bad_reader,
        now=NOW,
    )
    assert result.status is AdapterStatus.OK
    by_id = _by_id(result)
    assert by_id["comp-x"].state is State.UNREPORTED
    assert "body" in result.unavailable


def test_lister_failure_is_failed_not_empty():
    def boom(b, p):
        raise RuntimeError("bucket unreachable")

    result = checks_envelope.fetch(
        _cfg(), lister=boom, reader=_reader, now=NOW,
    )
    assert result.status is AdapterStatus.FAILED
    assert "source" in result.unavailable
    assert result.entities == ()


def test_missing_bucket_is_failed():
    result = checks_envelope.fetch(
        {"prefix": PREFIX}, lister=_lister, reader=_reader, now=NOW,
    )
    assert result.status is AdapterStatus.FAILED


def test_no_lister_or_reader_is_failed():
    result = checks_envelope.fetch(_cfg(), now=NOW)
    assert result.status is AdapterStatus.FAILED
    assert "lister" in result.unavailable
    assert "reader" in result.unavailable


def test_unmatched_keys_skipped():
    result = checks_envelope.fetch(
        _cfg(),
        lister=lambda b, p: [("other/path/file.txt", "2026-07-31T11:00:00Z")],
        reader=_reader,
        now=NOW,
    )
    assert result.entities == ()


def test_body_without_check_id_falls_back_to_path_group():
    def reader(b, k):
        body = dict(OK_BODY)
        del body["check_id"]
        return body

    result = checks_envelope.fetch(
        _cfg(),
        lister=lambda b, p: [(f"{PREFIX}from-path/latest.json", None)],
        reader=reader,
        now=NOW,
    )
    components = [e for e in result.entities if e.kind is Kind.COMPONENT]
    assert [c.id for c in components] == ["from-path"]


def test_checks_envelope_is_registered():
    assert "checks-envelope" in ADAPTERS
