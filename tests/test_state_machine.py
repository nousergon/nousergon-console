"""state-machine adapter tests — recorded execution fixtures, no live AWS.

Fixtures use synthetic ARNs and names only (this repo is public, AGPL — no
fleet topology anywhere, tests included). Records are shaped like a Step
Functions ListExecutions + DescribeExecution projection.
"""
from __future__ import annotations

from datetime import datetime, timezone

from console.adapters import state_machine
from console.config import ADAPTERS
from console.model.envelope import AdapterStatus
from console.model.kinds import Kind, State

ARN_A = "arn:aws:states:xx-test-1:000000000000:stateMachine:fixture-pipeline"
ARN_B = "arn:aws:states:xx-test-1:000000000000:stateMachine:fixture-other"

EXEC_OK = {
    "executionArn": "arn:aws:states:xx-test-1:000000000000:execution:fixture-pipeline:run-ok",
    "name": "run-ok",
    "status": "SUCCEEDED",
    "startDate": datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc),
    "stopDate": datetime(2026, 7, 31, 9, 12, tzinfo=timezone.utc),
    "input": (
        '{"execution_date": "2026-07-31",'
        ' "output_key": "s3://fixture-bucket/out/2026-07-31.json"}'
    ),
    "output": '{"artifact_key": "s3://fixture-bucket/out/2026-07-31.json"}',
}
EXEC_FAIL = {
    "executionArn": "arn:aws:states:xx-test-1:000000000000:execution:fixture-pipeline:run-fail",
    "name": "run-fail",
    "status": "FAILED",
    "startDate": "2026-07-30T09:00:00Z",
    "stopDate": "2026-07-30T09:05:00Z",
    "input": {"execution_date": "2026-07-30"},
    "output": None,
}
EXEC_RUNNING = {
    "executionArn": "arn:aws:states:xx-test-1:000000000000:execution:fixture-pipeline:run-live",
    "name": "run-live",
    "status": "RUNNING",
    "startDate": datetime(2026, 7, 31, 11, 0, tzinfo=timezone.utc),
    "stopDate": None,
    "input": {"execution_date": "2026-07-31"},
}


def _reader_for(mapping):
    def reader(region, arn):
        assert region == "xx-test-1"
        return list(mapping.get(arn, []))
    return reader


def _cfg(**extra):
    return {
        "region": "xx-test-1",
        "state_machines": [ARN_A],
        "cycle_key": "execution_date",
        **extra,
    }


def _by_id(result):
    return {e.id: e for e in result.entities}


def test_executions_become_runs_keyed_by_arn():
    result = state_machine.fetch(
        _cfg(), reader=_reader_for({ARN_A: [EXEC_OK, EXEC_FAIL, EXEC_RUNNING]}),
    )
    assert result.status is AdapterStatus.OK
    runs = {e.id: e for e in result.entities if e.kind is Kind.RUN}
    assert set(runs) == {
        EXEC_OK["executionArn"], EXEC_FAIL["executionArn"], EXEC_RUNNING["executionArn"],
    }
    assert runs[EXEC_OK["executionArn"]].state is State.HEALTHY
    assert runs[EXEC_FAIL["executionArn"]].state is State.FAILED
    # §8.3 added a thirteenth state, RUNNING, for exactly this case
    # (alpha-engine-config-I6358): a RUNNING execution has neither failed nor
    # finished. HEALTHY claims an ending that has not happened; STALLED
    # claims a stale heartbeat before one exists; UNREPORTED would put every
    # normal execution on the exception list and inflate the
    # transparency-gap count, whose objective is zero.
    assert runs[EXEC_RUNNING["executionArn"]].state is State.RUNNING
    assert runs[EXEC_RUNNING["executionArn"]].detail["status"] == "RUNNING"


def test_cycle_key_produces_cycle_entities_and_belongs_to_edges():
    result = state_machine.fetch(
        _cfg(), reader=_reader_for({ARN_A: [EXEC_OK, EXEC_FAIL, EXEC_RUNNING]}),
    )
    cycles = {e.id: e for e in result.entities if e.kind is Kind.CYCLE}
    assert set(cycles) == {"2026-07-31", "2026-07-30"}
    # 2026-07-31 has OK + RUNNING — RUNNING outranks HEALTHY (not yet resolved)
    assert cycles["2026-07-31"].state is State.RUNNING
    # 2026-07-30 is the failed run alone
    assert cycles["2026-07-30"].state is State.FAILED
    rels = {(e.source, e.rel, e.target) for e in result.edges}
    assert (EXEC_OK["executionArn"], "belongs-to", "2026-07-31") in rels
    assert (EXEC_FAIL["executionArn"], "belongs-to", "2026-07-30") in rels


def test_durable_keys_in_input_output_become_artifacts():
    result = state_machine.fetch(
        _cfg(), reader=_reader_for({ARN_A: [EXEC_OK]}),
    )
    arts = [e for e in result.entities if e.kind is Kind.ARTIFACT]
    keys = {e.id for e in arts}
    assert "s3://fixture-bucket/out/2026-07-31.json" in keys
    rels = {(e.source, e.rel, e.target) for e in result.edges}
    assert (EXEC_OK["executionArn"], "produces",
            "s3://fixture-bucket/out/2026-07-31.json") in rels


def test_input_key_field_becomes_consumed_by_edge():
    """nousergon-console#52: an input_key field names something the run
    READS — declared consumed-by the run, the reverse direction from
    produces. The existing output_key/artifact_key fixture (EXEC_OK) must
    keep producing a `produces` edge unchanged (regression guard below)."""
    rec = {
        **EXEC_OK,
        "input": (
            '{"execution_date": "2026-07-31",'
            ' "input_key": "s3://fixture-bucket/in/2026-07-31.json"}'
        ),
    }
    result = state_machine.fetch(_cfg(), reader=_reader_for({ARN_A: [rec]}))
    rels = {(e.source, e.rel, e.target) for e in result.edges}
    assert ("s3://fixture-bucket/in/2026-07-31.json", "consumed-by",
            EXEC_OK["executionArn"]) in rels
    # No produces edge was invented for the same key.
    assert (EXEC_OK["executionArn"], "produces",
            "s3://fixture-bucket/in/2026-07-31.json") not in rels


def test_input_key_artifact_is_relation_reachable_via_the_run():
    """The run itself becomes relation-reachable through ONLY this adapter's
    own declared edge — no reliance on checks_envelope/yaml_directory."""
    from console.index.graph import Index

    rec = {
        **EXEC_OK,
        "input": (
            '{"execution_date": "2026-07-31",'
            ' "input_key": "s3://fixture-bucket/in/2026-07-31.json"}'
        ),
    }
    result = state_machine.fetch(_cfg(), reader=_reader_for({ARN_A: [rec]}))
    index = Index()
    index.add_result(result)
    index.finalize()
    inbound = index.inbound(EXEC_OK["executionArn"])
    assert any(e.rel == "consumes" for e in inbound), (
        "the run must be relation-reachable (§3.1) via the derived reverse "
        "of the consumed-by edge this adapter now declares"
    )


def test_output_key_and_artifact_key_still_produce_not_consume():
    """Regression guard: the pre-existing EXEC_OK fixture's output_key
    (inside input) and artifact_key (inside output) both name the SAME key
    and must both stay classified as produced — field name overrides which
    payload (input vs output) carried them."""
    result = state_machine.fetch(_cfg(), reader=_reader_for({ARN_A: [EXEC_OK]}))
    rels = {(e.source, e.rel, e.target) for e in result.edges}
    key = "s3://fixture-bucket/out/2026-07-31.json"
    assert (EXEC_OK["executionArn"], "produces", key) in rels
    assert not any(e.rel == "consumed-by" and e.source == key for e in result.edges)


def test_horizon_is_declared_on_cycles_not_silently_truncated():
    """§3.4 / I3 gotcha: the adapter pages fully and declares the horizon it
    covered. A cycle carries earliest/latest/count so a truncated history
    cannot pass as complete."""
    result = state_machine.fetch(
        _cfg(), reader=_reader_for({ARN_A: [EXEC_OK, EXEC_FAIL, EXEC_RUNNING]}),
    )
    cycles = [e for e in result.entities if e.kind is Kind.CYCLE]
    assert cycles, "expected at least one cycle to carry the horizon"
    for c in cycles:
        assert c.detail["horizon_execution_count"] == 3
        assert c.detail["horizon_machines_read"] == 1
        assert c.detail["horizon_machines_configured"] == 1
        assert c.detail["horizon_complete"] is True
        assert c.detail["horizon_earliest"] is not None
        assert c.detail["horizon_latest"] is not None


def test_cost_is_declared_unavailable():
    result = state_machine.fetch(
        _cfg(), reader=_reader_for({ARN_A: [EXEC_OK]}),
    )
    assert "cost" in result.unavailable


def test_multi_machine_reader_is_called_per_arn():
    seen = []

    def reader(region, arn):
        seen.append(arn)
        return [EXEC_OK] if arn == ARN_A else [EXEC_FAIL]

    result = state_machine.fetch(
        {**_cfg(), "state_machines": [ARN_A, ARN_B]}, reader=reader,
    )
    assert seen == [ARN_A, ARN_B]
    assert result.status is AdapterStatus.OK
    assert len([e for e in result.entities if e.kind is Kind.RUN]) == 2


def test_reader_failure_on_all_machines_is_failed():
    def boom(region, arn):
        raise RuntimeError("sf unreachable")

    result = state_machine.fetch(_cfg(), reader=boom)
    assert result.status is AdapterStatus.FAILED
    assert "source" in result.unavailable
    assert result.entities == ()


def test_partial_reader_failure_still_returns_ok_with_partial_flag():
    def flaky(region, arn):
        if arn == ARN_B:
            raise RuntimeError("denied")
        return [EXEC_OK]

    result = state_machine.fetch(
        {**_cfg(), "state_machines": [ARN_A, ARN_B]}, reader=flaky,
    )
    assert result.status is AdapterStatus.OK
    assert "partial-source" in result.unavailable
    assert any(e.kind is Kind.RUN for e in result.entities)


def test_missing_region_is_failed():
    result = state_machine.fetch(
        {"state_machines": [ARN_A]}, reader=_reader_for({}),
    )
    assert result.status is AdapterStatus.FAILED
    assert "region" in result.unavailable


def test_missing_machines_is_failed():
    result = state_machine.fetch(
        {"region": "xx-test-1", "state_machines": []},
        reader=_reader_for({}),
    )
    assert result.status is AdapterStatus.FAILED
    assert "state_machines" in result.unavailable


def test_no_reader_is_failed_not_empty(monkeypatch):
    # With no injectable reader the adapter falls back to the boto3 default.
    # Force the "boto3 absent" path so the test is hermetic on every host.
    monkeypatch.setattr(state_machine, "_default_reader", lambda: None)
    result = state_machine.fetch(_cfg())
    assert result.status is AdapterStatus.FAILED
    assert "reader" in result.unavailable


def test_empty_history_declares_executions_unavailable():
    result = state_machine.fetch(
        _cfg(), reader=_reader_for({ARN_A: []}),
    )
    assert result.status is AdapterStatus.OK
    assert "executions" in result.unavailable
    assert result.entities == ()


def test_record_without_id_is_skipped():
    result = state_machine.fetch(
        _cfg(), reader=_reader_for({ARN_A: [{"status": "SUCCEEDED"}]}),
    )
    assert result.entities == ()


def test_custom_cycle_key():
    rec = {
        **EXEC_OK,
        "input": {"trade_date": "2026-08-01"},
    }
    result = state_machine.fetch(
        {**_cfg(), "cycle_key": "trade_date"},
        reader=_reader_for({ARN_A: [rec]}),
    )
    cycles = {e.id for e in result.entities if e.kind is Kind.CYCLE}
    assert cycles == {"2026-08-01"}


def test_state_machine_is_registered():
    assert "state-machine" in ADAPTERS
