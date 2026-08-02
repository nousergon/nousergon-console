"""local-units adapter tests — recorded systemd fixtures, no live systemd.

Fixtures use synthetic unit names (this repo is public, AGPL — no fleet
topology anywhere, tests included). Records are shaped like
``systemctl show --output=json --timestamp=unix`` output.
"""
from __future__ import annotations

import pytest

from console.adapters import local_units
from console.config import ADAPTERS
from console.model.envelope import AdapterStatus
from console.model.kinds import Kind, State

RUNNING = {
    "Id": "comp-alpha.service",
    "Description": "Alpha engine component",
    "LoadState": "loaded",
    "ActiveState": "active",
    "SubState": "running",
    "UnitFileState": "enabled",
    "FragmentPath": "/etc/systemd/system/comp-alpha.service",
    "InvocationID": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    "ActiveEnterTimestamp": "1755327601",
    "Result": "success",
}
FAILED = {
    "Id": "comp-sweep.service",
    "Description": "Weekly sweep",
    "LoadState": "loaded",
    "ActiveState": "failed",
    "SubState": "failed",
    "UnitFileState": "enabled",
    "FragmentPath": "/etc/systemd/system/comp-sweep.service",
    "InvocationID": "11111111-2222-4333-8444-555555555555",
    "ActiveEnterTimestamp": "1755239400",
    "Result": "exit-code",
}
RESTING = {
    "Id": "comp-daily.timer",
    "Description": "Daily check",
    "LoadState": "loaded",
    "ActiveState": "inactive",
    "SubState": "waiting",
    "UnitFileState": "enabled",
    "FragmentPath": "/etc/systemd/system/comp-daily.timer",
    "InvocationID": "22222222-3333-4444-8555-666666666666",
    "ActiveEnterTimestamp": "1755154800",
    "Result": "success",
}
MASKED = {
    "Id": "comp-old.service",
    "Description": "Retired component",
    "LoadState": "masked",
    "ActiveState": "inactive",
    "SubState": "dead",
    "UnitFileState": "masked",
    "InvocationID": "",
    "ActiveEnterTimestamp": "",
}
FIXTURE_UNITS = [RUNNING, FAILED, RESTING, MASKED]


def _enumerate(prefixes):
    return FIXTURE_UNITS


def _cfg():
    return {"unit_prefixes": ["comp-"]}


def _by_id(result):
    return {e.id: e for e in result.entities}


def test_units_become_components_with_runs():
    result = local_units.fetch(_cfg(), enumerator=_enumerate)
    assert result.status is AdapterStatus.OK
    by_id = _by_id(result)
    components = {e.id for e in result.entities if e.kind is Kind.COMPONENT}
    runs = {e.id for e in result.entities if e.kind is Kind.RUN}
    assert components == {"comp-alpha.service", "comp-sweep.service",
                          "comp-daily.timer", "comp-old.service"}
    # Every unit with a recorded activation gets a run; the masked one has none.
    assert runs == {RUNNING["InvocationID"], FAILED["InvocationID"],
                    RESTING["InvocationID"]}
    assert by_id[RUNNING["InvocationID"]].kind is Kind.RUN


def test_run_belongs_to_its_unit_edge():
    result = local_units.fetch(_cfg(), enumerator=_enumerate)
    rels = {(e.source, e.rel, e.target) for e in result.edges}
    assert (RUNNING["InvocationID"], "belongs-to", "comp-alpha.service") in rels
    assert (RESTING["InvocationID"], "belongs-to", "comp-daily.timer") in rels


def test_active_unit_is_healthy_and_run_as_of_is_iso():
    result = local_units.fetch(_cfg(), enumerator=_enumerate)
    by_id = _by_id(result)
    comp = by_id["comp-alpha.service"]
    assert comp.state is State.HEALTHY
    assert comp.provenance.as_of == "2025-08-16T07:00:01+00:00"
    run = by_id[RUNNING["InvocationID"]]
    assert run.state is State.HEALTHY
    assert run.provenance.as_of == "2025-08-16T07:00:01+00:00"
    assert run.detail["unit"] == "comp-alpha.service"


def test_failed_unit_is_failing_including_its_run():
    result = local_units.fetch(_cfg(), enumerator=_enumerate)
    by_id = _by_id(result)
    assert by_id["comp-sweep.service"].state is State.FAILING
    assert by_id[FAILED["InvocationID"]].state is State.FAILING


def test_resting_timer_is_unknown_with_healthy_last_run():
    # Inactive-but-loaded: whether resting is a finding lives in the registry,
    # which this adapter must not read — the component is UNKNOWN, and the
    # last activation (which succeeded) is its own HEALTHY run.
    result = local_units.fetch(_cfg(), enumerator=_enumerate)
    by_id = _by_id(result)
    assert by_id["comp-daily.timer"].state is State.UNKNOWN
    assert by_id[RESTING["InvocationID"]].state is State.HEALTHY


def test_masked_unit_is_absent():
    # systemd itself supplies the operator intent for a masked unit: disabled.
    result = local_units.fetch(_cfg(), enumerator=_enumerate)
    by_id = _by_id(result)
    assert by_id["comp-old.service"].state is State.ABSENT


def test_unit_without_invocation_has_no_run(tmp_path):
    rec = {**RUNNING, "Id": "comp-other.service", "InvocationID": "",
           "ActiveEnterTimestamp": ""}
    result = local_units.fetch(_cfg(), enumerator=lambda p: [rec])
    by_id = _by_id(result)
    assert "comp-other.service" in by_id
    assert all(e.kind is Kind.COMPONENT for e in result.entities)


def test_finished_activation_with_bad_result_is_failing():
    rec = {**RESTING, "Id": "comp-bad.service", "InvocationID": "33333333-4444-4555-8666-777777777777",
           "Result": "timeout"}
    result = local_units.fetch(_cfg(), enumerator=lambda p: [rec])
    by_id = _by_id(result)
    assert by_id[rec["InvocationID"]].state is State.FAILING


def test_finished_activation_without_result_is_unknown():
    rec = {**RESTING, "Id": "comp-nr.service", "InvocationID": "44444444-5555-4666-8777-888888888888",
           "Result": ""}
    result = local_units.fetch(_cfg(), enumerator=lambda p: [rec])
    by_id = _by_id(result)
    assert by_id[rec["InvocationID"]].state is State.UNKNOWN


def test_unreadable_unit_is_unknown_declared():
    # The production enumerator falls back to {"Id": ...} when the record
    # cannot be read — a declared absence, never a dropped row (§2.3), and
    # the adapter says what it could not supply.
    rec = {"Id": "comp-x.service"}
    result = local_units.fetch(_cfg(), enumerator=lambda p: [rec])
    by_id = _by_id(result)
    assert by_id["comp-x.service"].state is State.UNKNOWN
    assert result.unavailable == ("activation",)


def test_record_without_id_is_skipped():
    result = local_units.fetch(_cfg(), enumerator=lambda p: [{"ActiveState": "active"}])
    assert result.entities == ()


def test_prefixes_are_passed_to_enumerator():
    # The configured selector is the enumerator's only input — the adapter
    # knows the prefix list came from config, never from source (§2.3).
    seen = []

    def spy(prefixes):
        seen.append(prefixes)
        return []

    result = local_units.fetch({"unit_prefixes": ["comp-", "ne-"]}, enumerator=spy)
    assert seen == [["comp-", "ne-"]]
    assert result.status is AdapterStatus.OK


def test_missing_prefixes_is_failed_not_empty():
    result = local_units.fetch({}, enumerator=_enumerate)
    assert result.status is AdapterStatus.FAILED
    assert "unit_prefixes" in result.unavailable


def test_non_list_prefixes_is_failed():
    result = local_units.fetch({"unit_prefixes": "comp-"}, enumerator=_enumerate)
    assert result.status is AdapterStatus.FAILED


def test_enumerator_failure_is_failed_not_empty():
    def boom(prefixes):
        raise RuntimeError("scheduler unreachable")

    result = local_units.fetch(_cfg(), enumerator=boom)
    assert result.status is AdapterStatus.FAILED
    assert "source" in result.unavailable
    assert result.entities == ()


def test_local_units_is_registered():
    assert "local-units" in ADAPTERS
