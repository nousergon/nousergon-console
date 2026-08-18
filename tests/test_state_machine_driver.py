"""`state-machine` DRIVER tests (`console/drivers/state_machine.py`) — one
component's own execution history bound from its own descriptor
(`nousergon-console#99`), no live AWS.

Fixtures use synthetic ARNs and names only (this repo is public, AGPL — no
fleet topology anywhere, tests included), mirroring
`tests/test_state_machine.py`'s adapter fixtures. This file exercises the
DRIVER direction: one component names one ARN via `runs:` binding, and the
mapping is read from the SAME `console/state_machine_shape.py::run_state`
function the adapter uses — asserted directly below rather than re-derived.
"""
from __future__ import annotations

from datetime import datetime, timezone

from console.drivers import DRIVERS, state_machine as driver
from console.model.descriptor import Binding
from console.model.kinds import Kind, State

ARN = "arn:aws:states:xx-test-1:000000000000:stateMachine:fixture-pipeline"
NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)

EXEC_OK = {
    "executionArn": "arn:aws:states:xx-test-1:000000000000:execution:fixture-pipeline:run-ok",
    "name": "run-ok",
    "status": "SUCCEEDED",
    "startDate": datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc),
    "stopDate": datetime(2026, 7, 31, 9, 12, tzinfo=timezone.utc),
}
EXEC_FAILED = {
    "executionArn": "arn:aws:states:xx-test-1:000000000000:execution:fixture-pipeline:run-failed",
    "name": "run-failed",
    "status": "FAILED",
    "startDate": "2026-07-30T09:00:00Z",
    "stopDate": "2026-07-30T09:05:00Z",
}
EXEC_ABORTED = {
    "executionArn": "arn:aws:states:xx-test-1:000000000000:execution:fixture-pipeline:run-aborted",
    "name": "run-aborted",
    "status": "ABORTED",
    "startDate": "2026-07-29T09:00:00Z",
    "stopDate": "2026-07-29T09:05:00Z",
}
EXEC_TIMED_OUT = {
    "executionArn": "arn:aws:states:xx-test-1:000000000000:execution:fixture-pipeline:run-timeout",
    "name": "run-timeout",
    "status": "TIMED_OUT",
    "startDate": "2026-07-28T09:00:00Z",
    "stopDate": "2026-07-28T10:05:00Z",
}
EXEC_RUNNING = {
    "executionArn": "arn:aws:states:xx-test-1:000000000000:execution:fixture-pipeline:run-live",
    "name": "run-live",
    "status": "RUNNING",
    "startDate": datetime(2026, 7, 31, 11, 0, tzinfo=timezone.utc),
    "stopDate": None,
}
EXEC_PENDING = {
    "executionArn": "arn:aws:states:xx-test-1:000000000000:execution:fixture-pipeline:run-pending",
    "name": "run-pending",
    "status": "PENDING",
    "startDate": "2026-07-31T11:00:00Z",
    "stopDate": None,
}
EXEC_PENDING_REDRIVE = {
    "executionArn": "arn:aws:states:xx-test-1:000000000000:execution:fixture-pipeline:run-redrive",
    "name": "run-redrive",
    "status": "PENDING_REDRIVE",
    "startDate": "2026-07-31T11:00:00Z",
    "stopDate": None,
}
EXEC_UNKNOWN = {
    "executionArn": "arn:aws:states:xx-test-1:000000000000:execution:fixture-pipeline:run-mystery",
    "name": "run-mystery",
    "status": "SOMETHING_NEW",
    "startDate": "2026-07-31T11:00:00Z",
    "stopDate": "2026-07-31T11:05:00Z",
}

ALL_EXECS = [
    EXEC_OK, EXEC_FAILED, EXEC_ABORTED, EXEC_TIMED_OUT,
    EXEC_RUNNING, EXEC_PENDING, EXEC_PENDING_REDRIVE, EXEC_UNKNOWN,
]


def _binding(spec):
    return Binding(component_id="comp-x", kind="runs", driver="state-machine", spec=spec)


def _reader_for(mapping):
    def reader(region, arn):
        assert region == "xx-test-1"
        return list(mapping.get(arn, []))
    return reader


def _by_id(result):
    return {e.id: e for e in result.entities}


def test_state_machine_is_registered():
    assert "state-machine" in DRIVERS
    assert DRIVERS["state-machine"] is driver


def test_missing_arn_is_failed():
    result = driver.read(_binding({"region": "xx-test-1"}), {})
    assert result.ok is False
    assert "state_machine_arn" in result.unavailable


def test_missing_region_is_failed():
    result = driver.read(_binding({"state_machine_arn": ARN}), {})
    assert result.ok is False
    assert "region" in result.unavailable


def test_no_reader_available_is_failed(monkeypatch):
    monkeypatch.setattr(driver, "_default_reader", lambda: None)
    result = driver.read(
        _binding({"state_machine_arn": ARN, "region": "xx-test-1"}), {},
    )
    assert result.ok is False
    assert "reader" in result.unavailable


def test_reader_failure_is_failed():
    def boom(region, arn):
        raise RuntimeError("sf unreachable")

    result = driver.read(
        _binding({"state_machine_arn": ARN, "region": "xx-test-1"}),
        {"execution_reader": boom},
    )
    assert result.ok is False
    assert "sf unreachable" in result.error


def test_thirteen_state_mapping_over_fixture_executions():
    """Closes-when (`nousergon-console#99`, extended `alpha-engine-config-I6358`):
    a fixture execution list asserts the thirteen-state mapping —
    SUCCEEDED->HEALTHY, FAILED/ABORTED->FAILED, TIMED_OUT->STALLED,
    RUNNING/PENDING/PENDING_REDRIVE->RUNNING with source status carried in
    detail, unknown->UNREPORTED."""
    result = driver.read(
        _binding({"state_machine_arn": ARN, "region": "xx-test-1"}),
        {"execution_reader": _reader_for({ARN: ALL_EXECS})},
    )
    assert result.ok is True
    runs = _by_id(result)
    assert runs[EXEC_OK["executionArn"]].state is State.HEALTHY
    assert runs[EXEC_FAILED["executionArn"]].state is State.FAILED
    assert runs[EXEC_ABORTED["executionArn"]].state is State.FAILED
    assert runs[EXEC_TIMED_OUT["executionArn"]].state is State.STALLED
    assert runs[EXEC_RUNNING["executionArn"]].state is State.RUNNING
    assert runs[EXEC_RUNNING["executionArn"]].detail["status"] == "RUNNING"
    assert runs[EXEC_PENDING["executionArn"]].state is State.RUNNING
    assert runs[EXEC_PENDING["executionArn"]].detail["status"] == "PENDING"
    assert runs[EXEC_PENDING_REDRIVE["executionArn"]].state is State.RUNNING
    assert runs[EXEC_PENDING_REDRIVE["executionArn"]].detail["status"] == "PENDING_REDRIVE"
    assert runs[EXEC_UNKNOWN["executionArn"]].state is State.UNREPORTED
    for e in result.entities:
        assert e.kind is Kind.RUN


def test_entities_carry_declared_by_and_source():
    result = driver.read(
        _binding({"state_machine_arn": ARN, "region": "xx-test-1"}),
        {"execution_reader": _reader_for({ARN: [EXEC_OK]})},
    )
    run = result.entities[0]
    assert run.detail["declared_by"] == "comp-x"
    assert run.provenance.source == f"state-machine:{ARN}"
    assert run.detail["state_machine_arn"] == ARN
    assert run.detail["region"] == "xx-test-1"


def test_no_cycle_or_artifact_entities_emitted():
    """Unlike the adapter, the driver emits Run entities only — no
    Cycle/Artifact/horizon-honesty machinery (`nousergon-console#99`)."""
    result = driver.read(
        _binding({"state_machine_arn": ARN, "region": "xx-test-1"}),
        {"execution_reader": _reader_for({ARN: ALL_EXECS})},
    )
    assert all(e.kind is Kind.RUN for e in result.entities)
    assert result.edges == ()


def test_record_without_id_is_partial():
    result = driver.read(
        _binding({"state_machine_arn": ARN, "region": "xx-test-1"}),
        {"execution_reader": _reader_for({ARN: [{"status": "SUCCEEDED"}]})},
    )
    assert result.ok is True
    assert result.entities == ()
    assert "record" in result.unavailable


def test_cadence_minutes_declared():
    result = driver.read(
        _binding({
            "state_machine_arn": ARN, "region": "xx-test-1",
            "cadence_minutes": 1440,
        }),
        {"execution_reader": _reader_for({ARN: [EXEC_OK]})},
    )
    assert result.cadence_seconds == 1440.0 * 60.0


def test_adapter_and_driver_share_the_state_mapping():
    """Both callers import `run_state` from `console/state_machine_shape.py`
    — the adapter must not carry a forked copy of the mapping."""
    from console.adapters import state_machine as adapter
    from console.state_machine_shape import run_state

    assert adapter._run_state is run_state
    assert driver.run_state is run_state
