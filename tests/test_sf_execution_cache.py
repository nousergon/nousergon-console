"""The terminal-execution cache — a finished Step Functions execution is
described and history-read ONCE per process; only a RUNNING (or redriven)
execution is re-read, and new executions arrive through ``list_executions``.

Driven through the real boto3-backed readers (``state-machine`` adapter and
driver, and pipeline-reliability's history attach) against a fake Step
Functions client, so what is proven is the production read path, not just the
class. CI installs only the ``test`` extra, so boto3/botocore are faked here.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timezone

import pytest

from console import sf_execution_cache
from console.adapters import pipeline_reliability as pr
from console.adapters import state_machine as sm_adapter
from console.drivers import state_machine as sm_driver

REGION = "xx-test-1"
SM_ARN = "arn:aws:states:xx-test-1:000000000000:stateMachine:fixture-weekly"


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class FakeSFN:
    """Just enough of the Step Functions client: executions are mutable so a
    test can finish, redrive or expire one between passes."""

    def __init__(self) -> None:
        self.executions: dict[str, dict] = {}
        self.history: dict[str, list[str]] = {}
        self.describes: list[str] = []
        self.histories: list[str] = []
        self.lists = 0

    def add(self, name, status, start, stop=None, role="weekly", entered=(), redrive=0):
        arn = f"arn:aws:states:xx-test-1:000000000000:execution:fixture-weekly:{name}"
        self.executions[arn] = {
            "executionArn": arn, "stateMachineArn": SM_ARN, "name": name,
            "status": status, "startDate": _dt(start),
            **({"stopDate": _dt(stop)} if stop else {}),
            "redriveCount": redrive,
            "_input": json.dumps({"pipeline_role": role}),
        }
        self.history[arn] = list(entered)
        return arn

    def list_executions(self, **kwargs):
        assert kwargs["stateMachineArn"] == SM_ARN
        self.lists += 1
        return {"executions": [
            {k: v for k, v in e.items() if not k.startswith("_")}
            for e in self.executions.values()
        ]}

    def describe_execution(self, executionArn):  # noqa: N803 — boto3's spelling
        self.describes.append(executionArn.rsplit(":", 1)[-1])
        e = self.executions[executionArn]
        out = {k: v for k, v in e.items() if not k.startswith("_")}
        out["input"] = e["_input"]
        return out

    def get_execution_history(self, executionArn, maxResults, nextToken=None):  # noqa: N803
        self.histories.append(executionArn.rsplit(":", 1)[-1])
        return {"events": [
            {"stateEnteredEventDetails": {"name": n}} for n in self.history[executionArn]
        ]}


@pytest.fixture
def sfn(monkeypatch):
    exc = types.ModuleType("botocore.exceptions")
    exc.BotoCoreError = type("BotoCoreError", (Exception,), {})
    exc.ClientError = type("ClientError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "boto3", types.ModuleType("boto3"))
    monkeypatch.setitem(sys.modules, "botocore", types.ModuleType("botocore"))
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exc)
    fake = FakeSFN()
    for mod in (sm_adapter, sm_driver, pr):
        monkeypatch.setattr(mod, "_aws_client", lambda _service, _region=None: fake)
    sf_execution_cache.EXECUTION_CACHE.use_store(None)
    sf_execution_cache.EXECUTION_CACHE.clear()
    yield fake
    sf_execution_cache.EXECUTION_CACHE.use_store(None)
    sf_execution_cache.EXECUTION_CACHE.clear()


STAGES = ["WaitForCollectionManifests", "ResearchPredictorParallel"]


def _cfg():
    return {
        "region": REGION,
        "state_machines": [{
            "arn": SM_ARN, "pipeline_key": "weekly",
            "cadence": pr.CADENCE_WEEKDAY, "cadence_weekdays": [5],
            "stage_states": STAGES,
        }],
        "role_field": "pipeline_role",
        "cadence_roles": ["weekly"],
        "recovery_roles": ["watch-rerun", "recovery"],
        "window_trading_days": 4,
    }


NOW = datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)  # Saturday, evening


def _pass(fake):
    """One refresh pass of the pipeline-reliability adapter, default readers."""
    fake.describes.clear()
    fake.histories.clear()
    return pr.fetch(_cfg(), trading_day_checker=lambda d: False, now=NOW)


def _render(result):
    return sorted(
        (e.id, str(e.state), json.dumps(e.detail, sort_keys=True, default=str))
        for e in result.entities
    )


def test_terminal_execution_is_not_refetched_on_the_next_pass(sfn):
    sfn.add("w-0919", "SUCCEEDED", "2026-09-19T09:00:00", "2026-09-19T12:00:00", entered=STAGES)
    sfn.add("w-0926", "FAILED", "2026-09-26T09:00:00", "2026-09-26T09:10:00", entered=STAGES[:1])

    first = _pass(sfn)
    assert sorted(sfn.describes) == ["w-0919", "w-0926"]
    assert sorted(sfn.histories) == ["w-0919", "w-0926"]

    second = _pass(sfn)
    assert sfn.describes == []
    assert sfn.histories == []
    # The cache is a transport saving only: the rendered strip is identical.
    assert _render(second) == _render(first)


def test_running_execution_is_refetched_every_pass_until_it_finishes(sfn):
    sfn.add("w-0926", "SUCCEEDED", "2026-09-26T09:00:00", "2026-09-26T12:00:00", entered=STAGES)
    arn = sfn.add("w-1003", "RUNNING", "2026-10-03T09:00:00", entered=STAGES[:1])

    _pass(sfn)
    assert sorted(sfn.describes) == ["w-0926", "w-1003"]
    assert sorted(sfn.histories) == ["w-0926", "w-1003"]

    _pass(sfn)
    assert sfn.describes == ["w-1003"]
    assert sfn.histories == ["w-1003"]

    # It finishes: the next pass must re-read it (its listing tag changed),
    # and the one after that must not.
    sfn.executions[arn].update(status="SUCCEEDED", stopDate=_dt("2026-10-03T12:00:00"))
    sfn.history[arn] = list(STAGES)
    third = _pass(sfn)
    assert sfn.describes == ["w-1003"]
    assert sfn.histories == ["w-1003"]
    cycle = next(e for e in third.entities if e.detail.get("date") == "2026-10-03")
    assert cycle.detail["stage_reached"] == STAGES[-1]

    _pass(sfn)
    assert sfn.describes == []
    assert sfn.histories == []


def test_redriven_execution_is_reread(sfn):
    arn = sfn.add("w-0926", "FAILED", "2026-09-26T09:00:00", "2026-09-26T09:10:00",
                  entered=STAGES[:1])
    _pass(sfn)
    _pass(sfn)
    assert sfn.describes == [] and sfn.histories == []

    # RedriveExecution keeps the ARN; status goes back to RUNNING.
    sfn.executions[arn].update(status="RUNNING", redriveCount=1)
    sfn.executions[arn].pop("stopDate")
    _pass(sfn)
    assert sfn.describes == ["w-0926"] and sfn.histories == ["w-0926"]

    # ...and FAILS again with a new stopDate: re-read once more, then cached.
    sfn.executions[arn].update(status="SUCCEEDED", stopDate=_dt("2026-09-27T01:00:00"))
    _pass(sfn)
    assert sfn.describes == ["w-0926"] and sfn.histories == ["w-0926"]
    _pass(sfn)
    assert sfn.describes == [] and sfn.histories == []


def test_execution_gone_from_the_listing_leaves_the_cache(sfn):
    sfn.add("w-0919", "SUCCEEDED", "2026-09-19T09:00:00", "2026-09-19T12:00:00", entered=STAGES)
    old = sfn.add("w-0912", "SUCCEEDED", "2026-09-12T09:00:00", "2026-09-12T12:00:00",
                  entered=STAGES)
    _pass(sfn)
    assert len(sf_execution_cache.EXECUTION_CACHE) == 4  # 2 describes + 2 histories

    del sfn.executions[old]  # aged out of SF retention
    _pass(sfn)
    assert len(sf_execution_cache.EXECUTION_CACHE) == 2


def test_unreadable_history_is_not_cached(sfn):
    sfn.add("w-0926", "SUCCEEDED", "2026-09-26T09:00:00", "2026-09-26T12:00:00", entered=STAGES)
    real = sfn.get_execution_history
    ClientError = sys.modules["botocore.exceptions"].ClientError

    def boom(**kwargs):
        sfn.histories.append("boom")
        raise ClientError("throttled")

    sfn.get_execution_history = boom
    _pass(sfn)
    sfn.get_execution_history = real
    _pass(sfn)
    assert sfn.histories == ["w-0926"]  # retried, because the failure was not cached


def test_state_machine_driver_reader_shares_the_cache(sfn):
    sfn.add("w-0926", "SUCCEEDED", "2026-09-26T09:00:00", "2026-09-26T12:00:00")
    sfn.add("w-1003", "RUNNING", "2026-10-03T09:00:00")
    reader = sm_driver._default_reader()
    first = reader(REGION, SM_ARN)
    assert sorted(sfn.describes) == ["w-0926", "w-1003"]
    sfn.describes.clear()
    second = reader(REGION, SM_ARN)
    assert sfn.describes == ["w-1003"]
    assert first == second


def test_change_tag_is_none_unless_terminal_with_stop_date():
    tag = sf_execution_cache.change_tag
    assert tag({"status": "RUNNING", "stopDate": "x"}) is None
    assert tag({"status": "PENDING_REDRIVE", "stopDate": "x"}) is None
    assert tag({"status": "SUCCEEDED"}) is None
    assert tag({"status": "ABORTED", "stopDate": "x"}) == ("ABORTED", "x", "0")
    assert tag({"status": "TIMED_OUT", "stopDate": "x", "redriveCount": 2}) == \
        ("TIMED_OUT", "x", "2")


# ── persisted across a restart (alpha-engine-config-I11792) ──────────────────
#
# Measured 2026-10-14: every console restart re-read every retained execution's
# history (~400 GetExecutionHistory calls, mostly 1000-event weekly pages), and
# seven restarts in an afternoon made that ~800 calls an hour. A restart is
# modelled here as dropping the in-memory cache and re-pointing it at the same
# store — exactly what a new process sees.


def _restart(path):
    cache = sf_execution_cache.EXECUTION_CACHE
    cache.use_store(None)
    cache.clear()
    cache.use_store(str(path))


def test_terminal_execution_is_not_refetched_after_a_restart(sfn, tmp_path):
    store = tmp_path / "sf_execution_cache.json"
    sf_execution_cache.EXECUTION_CACHE.use_store(str(store))
    sfn.add("w-0919", "SUCCEEDED", "2026-09-19T09:00:00", "2026-09-19T12:00:00", entered=STAGES)
    sfn.add("w-0926", "FAILED", "2026-09-26T09:00:00", "2026-09-26T09:10:00", entered=STAGES[:1])
    sfn.add("w-1003", "RUNNING", "2026-10-03T09:00:00", entered=STAGES[:1])

    first = _pass(sfn)
    assert sorted(sfn.histories) == ["w-0919", "w-0926", "w-1003"]
    assert store.exists()

    _restart(store)
    second = _pass(sfn)
    # Only the RUNNING execution is re-read; the finished ones come off disk.
    assert sfn.describes == ["w-1003"]
    assert sfn.histories == ["w-1003"]
    assert _render(second) == _render(first)


def test_restored_describe_keeps_its_datetimes(sfn, tmp_path):
    store = tmp_path / "c.json"
    sf_execution_cache.EXECUTION_CACHE.use_store(str(store))
    sfn.add("w-0926", "SUCCEEDED", "2026-09-26T09:00:00", "2026-09-26T12:00:00")
    reader = sm_driver._default_reader()
    first = reader(REGION, SM_ARN)

    _restart(store)
    sfn.describes.clear()
    second = reader(REGION, SM_ARN)
    assert sfn.describes == []
    assert second == first
    assert isinstance(second[0]["stopDate"], datetime)


def test_execution_redriven_while_down_is_reread_after_restart(sfn, tmp_path):
    store = tmp_path / "c.json"
    sf_execution_cache.EXECUTION_CACHE.use_store(str(store))
    arn = sfn.add("w-0926", "FAILED", "2026-09-26T09:00:00", "2026-09-26T09:10:00",
                  entered=STAGES[:1])
    _pass(sfn)

    sfn.executions[arn].update(status="SUCCEEDED", redriveCount=1,
                               stopDate=_dt("2026-09-27T01:00:00"))
    sfn.history[arn] = list(STAGES)
    _restart(store)
    _pass(sfn)
    assert sfn.describes == ["w-0926"] and sfn.histories == ["w-0926"]


def test_execution_gone_while_down_is_dropped_from_the_store(sfn, tmp_path):
    store = tmp_path / "c.json"
    sf_execution_cache.EXECUTION_CACHE.use_store(str(store))
    sfn.add("w-0919", "SUCCEEDED", "2026-09-19T09:00:00", "2026-09-19T12:00:00", entered=STAGES)
    old = sfn.add("w-0912", "SUCCEEDED", "2026-09-12T09:00:00", "2026-09-12T12:00:00",
                  entered=STAGES)
    _pass(sfn)
    del sfn.executions[old]
    _restart(store)
    _pass(sfn)
    assert len(sf_execution_cache.EXECUTION_CACHE) == 2
    doc = json.loads(store.read_text())
    assert not any(a.endswith(":w-0912") for a in doc["details"])
    assert not any(a.endswith(":w-0912") for a in doc["entered"])


@pytest.mark.parametrize("body", [
    "{not json",
    json.dumps({"version": 999, "details": {}, "entered": {}}),
    json.dumps({"version": 1, "details": "nope"}),
])
def test_unreadable_store_is_a_cold_start_then_rewritten(sfn, tmp_path, body):
    store = tmp_path / "c.json"
    store.write_text(body)
    sf_execution_cache.EXECUTION_CACHE.use_store(str(store))
    sfn.add("w-0926", "SUCCEEDED", "2026-09-26T09:00:00", "2026-09-26T12:00:00", entered=STAGES)
    _pass(sfn)
    assert sfn.histories == ["w-0926"]
    assert json.loads(store.read_text())["version"] == sf_execution_cache.STORE_VERSION


def test_unwritable_store_never_fails_the_pass(sfn, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    sf_execution_cache.EXECUTION_CACHE.use_store(str(blocker / "sub" / "c.json"))
    sfn.add("w-0926", "SUCCEEDED", "2026-09-26T09:00:00", "2026-09-26T12:00:00", entered=STAGES)
    _pass(sfn)
    _pass(sfn)
    assert sfn.histories == []  # in-memory cache still works


def test_without_a_store_nothing_is_written(sfn, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sfn.add("w-0926", "SUCCEEDED", "2026-09-26T09:00:00", "2026-09-26T12:00:00", entered=STAGES)
    _pass(sfn)
    assert list(tmp_path.iterdir()) == []
    assert sf_execution_cache.EXECUTION_CACHE.persist() is False


def test_default_store_path(monkeypatch):
    monkeypatch.delenv(sf_execution_cache.STORE_PATH_ENV, raising=False)
    monkeypatch.delenv("CACHE_DIRECTORY", raising=False)
    assert sf_execution_cache.default_store_path() is None
    monkeypatch.setenv("CACHE_DIRECTORY", "/var/cache/nousergon-console:/var/cache/other")
    assert sf_execution_cache.default_store_path() == \
        "/var/cache/nousergon-console/sf_execution_cache.json"
    monkeypatch.setenv(sf_execution_cache.STORE_PATH_ENV, "/x/y.json")
    assert sf_execution_cache.default_store_path() == "/x/y.json"


def test_service_unit_gives_the_cache_a_directory():
    from pathlib import Path

    unit = Path(__file__).resolve().parents[1] / "infrastructure" / "nousergon-console.service"
    lines = [ln.strip() for ln in unit.read_text().splitlines()]
    assert "CacheDirectory=nousergon-console" in lines
