"""Build-stage spans and source read timings (alpha-engine-config-I13957).

The live index measured build_seconds=2045 while the sum of every measured
source read was 281, with nine sources null. These tests pin the contract that
makes the difference attributable: each read that happens is timed whether it
succeeds or fails, each stage is a wall-clock span with a defined boundary,
nothing that did not happen is reported as 0, and overlapping source durations
are never offered as build time.
"""
from __future__ import annotations

import threading
from pathlib import Path
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from console import config as cfg_mod
from console.config import build_index
from console.drivers import DRIVERS, resolve_bindings
from console.drivers.base import DriverResult
from console.index import actions as your_actions
from console.index import baseline as exceptions_baseline
from console.index import board as planning_board
from console.index import ticks as tick_lists
from console.index import triage as triage_overlay
from console.index import warm_start
from console.index.build import STAGE_ORDER, BuildStage, Supervisor
from console.index.graph import Index
from console.model.descriptor import Binding, Descriptor
from console.model.envelope import AdapterResult, AdapterStatus
from console.render import json as render_json


def _stages(index: Index) -> dict[str, dict[str, Any]]:
    rows = render_json.index_freshness(index)["stages"]
    return {r["name"]: r for r in rows}


def _sources(index: Index) -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in render_json.index_freshness(index)["sources"]}


def _failed(name: str) -> AdapterResult:
    return AdapterResult(name=name, status=AdapterStatus.FAILED,
                         unavailable=("unreadable",))


def test_every_stage_is_listed_and_unrun_ones_are_null_not_zero() -> None:
    stages = _stages(build_index({}))
    assert list(stages) == list(STAGE_ORDER)
    for name in ("config_parse", "registry_read", "adapter_fetch",
                 "driver_bindings", "onboarding_cost", "answer_latency",
                 "milestone_journal", "landing_model"):
        assert stages[name]["seconds"] is not None and stages[name]["seconds"] >= 0
        assert stages[name]["outcome"] == "ok"
    # Nothing declared, nothing read: null, and never 0.0.
    for name in ("warm_restore", "planner_reads", "planning_board_read",
                 "your_actions_read", "tick_lists_read", "triage_overlay_read",
                 "exceptions_baseline_read", "publish"):
        assert stages[name]["seconds"] is None and stages[name]["outcome"] is None
    assert all(r["definition"] for r in stages.values())
    assert stages["graph_fold"]["nested"] is True
    assert not any(r["nested"] for n, r in stages.items() if n != "graph_fold")


def test_graph_fold_is_measured_from_the_real_finalize() -> None:
    index = build_index({})
    fold = _stages(index)["graph_fold"]
    assert fold["seconds"] == pytest.approx(index.fold_seconds)
    assert fold["seconds"] >= 0


def test_previously_null_sources_are_timed_on_failed_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed_reader(name: str, shape: str) -> Callable[..., Any]:
        def read(*_a: Any, **_k: Any) -> Any:
            time.sleep(0.002)
            if shape == "pair":
                return {}, _failed(name)
            if shape == "list":
                return None, [_failed(name)]
            return {}, _failed(name)
        return read

    monkeypatch.setattr(planning_board, "parse", lambda _c: object())
    monkeypatch.setattr(planning_board, "read", failed_reader(planning_board.SOURCE_NAME, "list"))
    monkeypatch.setattr(planning_board, "attach", lambda *a, **k: None)
    monkeypatch.setattr(your_actions, "parse", lambda _c: object())
    monkeypatch.setattr(your_actions, "read", failed_reader(your_actions.SOURCE_NAME, "pair"))
    monkeypatch.setattr(your_actions, "attach", lambda *a, **k: None)
    monkeypatch.setattr(tick_lists, "parse", lambda _c: {"t": object()})
    monkeypatch.setattr(tick_lists, "read",
                        lambda *_a, **_k: ({}, [_failed(tick_lists.SOURCE_NAME)]))
    monkeypatch.setattr(tick_lists, "attach", lambda *a, **k: None)
    monkeypatch.setattr(cfg_mod.declared_pages, "parse", lambda *_a, **_k: [])
    monkeypatch.setattr(triage_overlay, "parse", lambda _c: object())
    monkeypatch.setattr(triage_overlay, "read", failed_reader(triage_overlay.SOURCE_NAME, "pair"))
    monkeypatch.setattr(exceptions_baseline, "parse", lambda _c: object())
    monkeypatch.setattr(exceptions_baseline, "read",
                        failed_reader(exceptions_baseline.SOURCE_NAME, "pair"))
    monkeypatch.setattr(exceptions_baseline, "read_trend",
                        lambda *_a, **_k: (None, _failed(exceptions_baseline.TREND_SOURCE_NAME)))

    index = build_index({})
    sources, stages = _sources(index), _stages(index)
    for name in (planning_board.SOURCE_NAME, your_actions.SOURCE_NAME,
                 tick_lists.SOURCE_NAME, triage_overlay.SOURCE_NAME,
                 exceptions_baseline.SOURCE_NAME,
                 exceptions_baseline.TREND_SOURCE_NAME):
        assert sources[name]["status"] == "failed"
        assert sources[name]["elapsed_seconds"] is not None, name
        assert sources[name]["elapsed_seconds"] >= 0
    for name in ("planning_board_read", "your_actions_read", "tick_lists_read",
                 "triage_overlay_read", "exceptions_baseline_read"):
        assert stages[name]["seconds"] is not None and stages[name]["outcome"] == "ok"


def test_outcomes_produced_without_a_read_keep_null_timing() -> None:
    index = build_index({"registry": {"adapter": "no-such-adapter", "name": "r"},
                         "adapters": [{"enabled": True, "kind": "nope", "name": "x"}]})
    sources = _sources(index)
    assert sources["r"]["status"] == "failed" and sources["r"]["elapsed_seconds"] is None
    assert sources["x"]["status"] == "failed" and sources["x"]["elapsed_seconds"] is None


def _descriptor(*bindings: Binding) -> Descriptor:
    return Descriptor(component_id="c", row={}, bindings=bindings)


def test_driver_reads_are_timed_on_success_and_failure_and_null_without_a_read(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def ok_read(binding: Binding, _ctx: dict) -> DriverResult:
        time.sleep(0.002)
        return DriverResult(binding=binding)

    def bad_read(binding: Binding, _ctx: dict) -> DriverResult:
        time.sleep(0.002)
        return DriverResult.failed(binding, "unreadable")

    monkeypatch.setitem(DRIVERS, "fake-ok", SimpleNamespace(name="fake-ok", read=ok_read))
    monkeypatch.setitem(DRIVERS, "fake-bad", SimpleNamespace(name="fake-bad", read=bad_read))
    descriptors = [_descriptor(
        Binding("c", "k", "fake-ok"), Binding("d", "k", "fake-ok"),
        Binding("c", "k", "fake-bad"), Binding("c", "k", "not-registered"))]
    seconds: dict[str, float] = {}
    results = {r.name: r for r in resolve_bindings(descriptors, timings=seconds)}
    assert results["driver:fake-ok"].status is AdapterStatus.OK
    assert results["driver:fake-bad"].status is AdapterStatus.FAILED
    assert seconds["fake-ok"] >= 0.004  # two reads, summed: they ran serially
    assert seconds["fake-bad"] > 0  # a failed read is timed too
    assert "not-registered" not in seconds  # no read happened: null, not 0
    # The original contract is untouched.
    assert [r.name for r in resolve_bindings(descriptors)] == sorted(results)


def test_driver_timing_reaches_the_source_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(DRIVERS, "fake-ok", SimpleNamespace(
        name="fake-ok", read=lambda b, _c: DriverResult(binding=b)))
    real = cfg_mod.resolve_bindings
    monkeypatch.setattr(cfg_mod, "resolve_bindings",
                        lambda d, c, timings=None: real(
                            [_descriptor(Binding("c", "k", "fake-ok"))], c,
                            timings=timings))
    assert _sources(build_index({}))["driver:fake-ok"]["elapsed_seconds"] is not None


def test_source_durations_overlap_and_are_not_build_wall_time(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cfg_mod.FETCH_WORKERS == 3
    live = peak = 0
    lock = threading.Lock()

    def slow(cfg: dict) -> AdapterResult:
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.15)
        with lock:
            live -= 1
        return AdapterResult(name=cfg["_name"], status=AdapterStatus.OK)

    monkeypatch.setitem(cfg_mod.ADAPTERS, "slow", SimpleNamespace(fetch=slow))
    index = build_index({"adapters": [
        {"enabled": True, "kind": "slow", "name": f"s{i}"} for i in range(6)]})
    assert peak == 3  # bounded concurrency, unchanged
    summed = sum(s["elapsed_seconds"] for n, s in _sources(index).items()
                 if n.startswith("s"))
    fetch_wall = _stages(index)["adapter_fetch"]["seconds"]
    assert summed >= 0.9 and fetch_wall < summed  # overlap: the sum is not wall time
    info = render_json.index_freshness(index)
    defs = info["timing_definitions"]
    assert "NOT build time" in defs["sources[].elapsed_seconds"]
    assert "overlap" in defs["sources[].elapsed_seconds"]
    assert "never computed by subtraction" in defs["stages[].seconds"]
    # No derived "unmeasured" figure is exposed anywhere in the status.
    assert not [k for k in info if "unmeasured" in k or "residual" in k]
    # Source order and outcomes are untouched.
    assert [n for n in _sources(index) if n.startswith("s")] == [f"s{i}" for i in range(6)]


def test_a_failing_stage_is_recorded_failed_and_rides_the_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_index: Index) -> dict:
        raise RuntimeError("questions broke")

    monkeypatch.setattr(cfg_mod, "measure_answer_latency", boom)
    with pytest.raises(RuntimeError) as err:
        build_index({})
    stages = {s.name: s for s in err.value.build_stages}  # type: ignore[attr-defined]
    assert stages["answer_latency"].outcome == "failed"
    assert stages["answer_latency"].seconds is not None
    assert stages["registry_read"].outcome == "ok"
    assert "landing_model" not in stages


def test_failed_build_keeps_the_served_index_and_shows_the_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(timezone.utc)
    sup = Supervisor(lambda: build_index({}), 180, clock=lambda: now)
    served = sup.current
    good_stages = _stages(served)
    assert good_stages["landing_model"]["seconds"] is not None
    assert render_json.index_freshness(served)["last_failed_stages"] is None

    monkeypatch.setattr(cfg_mod, "measure_answer_latency",
                        lambda _i: (_ for _ in ()).throw(RuntimeError("nope")))
    assert not sup.refresh_once()
    assert sup.current is served  # swap did not happen
    failed = {r["name"]: r for r in
              render_json.index_freshness(sup.current)["last_failed_stages"]}
    assert failed["answer_latency"]["outcome"] == "failed"
    assert failed["landing_model"]["seconds"] is None
    assert "RuntimeError" in (sup.current.build_info.last_error or "")

    monkeypatch.undo()
    assert sup.refresh_once()
    assert sup.current is not served
    assert render_json.index_freshness(sup.current)["last_failed_stages"] is None
    assert not sup.current.build_info.is_stale(now)


def test_a_failed_publish_is_a_failed_stage_and_does_not_fail_the_build(monkeypatch: pytest.MonkeyPatch) -> None:
    store = SimpleNamespace(declaration=SimpleNamespace(publish=("b", "k")),
                            publish_error="PutObject denied")
    monkeypatch.setattr(planning_board, "store", lambda _i: store)
    monkeypatch.setattr(planning_board, "publish", lambda *_a, **_k: None)
    stages = _stages(build_index({}))
    assert stages["publish"]["outcome"] == "failed"
    assert stages["publish"]["seconds"] is not None


def _supervisor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, seed: Index | None) -> Supervisor:
    monkeypatch.setattr(warm_start, "code_digest", lambda: "test")
    monkeypatch.setattr(warm_start, "load", lambda *_a: seed)
    monkeypatch.setattr(warm_start, "save", lambda *_a: True)
    return Supervisor(lambda: build_index({}), 180, defer_first_build=True,
                      warm_start_path=str(tmp_path / "cache"))


def test_warm_restart_records_the_restore_span_and_says_warm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seed = Index()
    sup = _supervisor(monkeypatch, tmp_path, seed)
    assert sup.refresh_once()
    info = render_json.index_freshness(sup.current)
    assert info["restart_cache"] == "warm"
    stages = _stages(sup.current)
    assert stages["warm_restore"]["seconds"] is not None
    assert stages["warm_restore"]["outcome"] == "ok"
    assert stages["landing_model"]["seconds"] is not None
    # Steady state: no restore, no restart label, and the span is null again.
    assert sup.refresh_once()
    assert render_json.index_freshness(sup.current)["restart_cache"] is None
    assert _stages(sup.current)["warm_restore"]["seconds"] is None


def test_cold_restart_is_labelled_cold(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup = _supervisor(monkeypatch, tmp_path, None)
    assert sup.refresh_once()
    assert render_json.index_freshness(sup.current)["restart_cache"] == "cold"
    assert _stages(sup.current)["warm_restore"]["seconds"] is not None


def test_a_failed_restore_is_a_failed_span_on_the_marker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sup = _supervisor(monkeypatch, tmp_path, None)
    monkeypatch.setattr(warm_start, "code_digest",
                        lambda: (_ for _ in ()).throw(OSError("digest unreadable")))
    assert not sup.refresh_once()
    failed = {r["name"]: r for r in
              render_json.index_freshness(sup.current)["last_failed_stages"]}
    assert failed["warm_restore"]["outcome"] == "failed"
    assert failed["warm_restore"]["seconds"] is not None
