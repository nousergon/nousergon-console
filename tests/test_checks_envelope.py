"""checks-envelope adapter tests — recorded envelopes, no live bucket.

Fixtures use synthetic check ids only (this repo is public; no fleet topology).
Envelope shape matches the fleet check-result contract (schema_version 1).
"""
from __future__ import annotations

from datetime import datetime, timezone

from console.adapters import checks_envelope
from console.config import ADAPTERS
from console.index.graph import Index
from console.model.envelope import AdapterStatus
from console.model.kinds import Kind, State
from console.render.html import is_exception, landing_exceptions

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
    assert by_id["comp-gamma"].state is State.FAILED


def test_stale_beats_last_ok_status():
    """A dying check's last write is almost always ok — ran_at + cadence mark
    it STALE when publishing stops (fleet_check_result contract)."""
    result = checks_envelope.fetch(
        _cfg(), lister=_lister, reader=_reader, now=NOW,
    )
    # Past its cadence the component is MISSED — the schedule fired or should
    # have and no run started, a failure UPSTREAM of the component. Not
    # STALLED (nothing reported a start), not FAILED (it never began).
    assert _by_id(result)["comp-stale"].state is State.MISSED


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
    # alpha-engine-config-I8768: the run's own `belongs-to` edge above makes
    # the COMPONENT relation-reachable (its reverse lands on the component,
    # `index/graph.py::_add_edge`), never the run itself — a run with only
    # that outbound edge has no inbound edge at all. The component's mirror
    # `produces` declaration is what closes it.
    (run,) = [e for e in result.entities if e.kind is Kind.RUN and e.id.startswith("comp-alpha@")]
    assert ("comp-alpha", "produces", run.id) in rels


def _build_index(result) -> Index:
    index = Index()
    index.add_result(result)
    return index


def test_artifact_state_is_freshness_not_the_envelope_status():
    """§5.2 (alpha-engine-config-I8979): the artifact's state is FRESH/STALE
    from last_modified vs cadence, never the verdict inside the body. A fresh
    `latest.json` reporting an `error` verdict is still a healthy artifact."""
    result = checks_envelope.fetch(
        _cfg(), lister=_lister, reader=_reader, now=NOW,
    )
    by_id = _by_id(result)
    gamma_component = by_id["comp-gamma"]
    gamma_artifact = by_id[f"{PREFIX}comp-gamma/latest.json"]
    assert gamma_component.state is State.FAILED
    assert gamma_artifact.state == "fresh"
    assert gamma_artifact.state != gamma_component.state_value


def test_stale_artifact_freshness_independent_of_ok_status():
    """comp-stale reports status ok (its component is MISSED on staleness) —
    the artifact's own freshness reasons from the SAME last_modified/cadence
    and lands on `stale`, not on the component's status or derived state."""
    result = checks_envelope.fetch(
        _cfg(), lister=_lister, reader=_reader, now=NOW,
    )
    by_id = _by_id(result)
    assert by_id[f"{PREFIX}comp-stale/latest.json"].state == "stale"


def test_degraded_envelope_collapses_to_one_exception_row():
    """The 133-of-139 defect: one DEGRADED envelope minted a component, a run
    and an artifact that all rendered as exceptions. Now only the component
    row (the verdict) lists; the fresh artifact and the redundant run do not."""
    result = checks_envelope.fetch(
        _cfg(), lister=_lister, reader=_reader, now=NOW,
    )
    index = _build_index(result)
    exceptions = landing_exceptions(index)
    beta_rows = [e for e in exceptions if e.detail.get("check_id") == "comp-beta"
                 or e.id == "comp-beta"]
    assert [e.kind for e in beta_rows] == [Kind.COMPONENT]
    # The run stays fully present and reachable in the index — only the
    # exception VIEW collapses (§4.3), never the underlying graph.
    (run,) = [e for e in index.all()
              if e.kind is Kind.RUN and e.id.startswith("comp-beta@")]
    assert run.state is State.DEGRADED
    assert is_exception(run)  # still an exception in isolation
    assert run not in exceptions  # but suppressed as a component echo
    edges = {(e.source, e.rel, e.target) for e in index.related(run.id)}
    assert (run.id, "belongs-to", "comp-beta") in edges


def test_transient_run_failure_under_healthy_component_still_lists():
    """A run that disagrees with its (now-healthy) component is a different
    fact — a transient — and must still list even though the component does
    not (alpha-engine-config-I8979)."""
    from console.model.entity import Edge, Entity, Provenance

    now_iso = "2026-07-31T12:00:00+00:00"
    component = Entity(
        kind=Kind.COMPONENT, id="comp-recovered", state=State.HEALTHY,
        provenance=Provenance(source="s3://x", as_of=now_iso, evidence="s3://x/comp-recovered"),
    )
    run = Entity(
        kind=Kind.RUN, id="comp-recovered@2026-07-31T11:00:00+00:00", state=State.FAILED,
        provenance=Provenance(source="s3://x", as_of="2026-07-31T11:00:00+00:00",
                               evidence="s3://x/comp-recovered"),
    )
    index = Index()
    from console.model.envelope import AdapterResult, ClaimClass
    index.add_result(AdapterResult(
        claim_class=ClaimClass.OBSERVATION, fetched_at=now_iso, name="checks",
        status=AdapterStatus.OK, entities=(component, run),
        edges=(Edge(source=run.id, rel="belongs-to", target=component.id),),
    ))
    exceptions = landing_exceptions(index)
    assert run in exceptions
    assert component not in exceptions


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


def test_no_lister_or_reader_is_failed(monkeypatch):
    # With no injectable lister/reader the adapter falls back to the boto3
    # default. Force the "boto3 absent" path so the test is hermetic.
    monkeypatch.setattr(checks_envelope, "_default_s3", lambda: (None, None))
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
