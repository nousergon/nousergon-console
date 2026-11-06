"""/exceptions marks rows new since yesterday's list and names cleared ones."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from console import config as console_config
from console.index import baseline
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import json as render_json
from console.server.app import _html
from console.server.router import resolve

SRC = baseline.BaselineSource(bucket="b", key="ops/console/exceptions-{date}.json")
NOW = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)
YESTERDAY = {"view": "exceptions", "snapshot_utc": "2026-10-31T21:24:39Z", "rows": [
    {"id": "ae-a", "label": "A"}, {"id": "ae-gone", "label": "Gone row"}]}


def _row(rid):
    return Entity(kind=Kind.COMPONENT, id=rid, state=State.FAILED,
                  provenance=Provenance("s3://b/x.json", "2026-11-01T10:00:00+00:00"))


def _index(doc=YESTERDAY):
    idx = Index()
    idx.add_result(AdapterResult(name="fixture", status=AdapterStatus.OK,
                                 claim_class=ClaimClass.OBSERVATION,
                                 entities=(_row("ae-a"), _row("ae-new"))))
    seen = {}

    def reader(src, key):
        seen["key"] = key
        return json.dumps(doc).encode()

    base, result = baseline.read(SRC, NOW, reader=reader)
    assert seen["key"] == "ops/console/exceptions-2026-10-31.json"
    idx.add_result(result)
    idx.set_exceptions_baseline(base)
    idx.finalize()
    return idx


def test_rows_are_marked_new_and_cleared_rows_are_named():
    idx = _index()
    doc = render_json.payload(idx, resolve("/exceptions"))
    assert {r["id"]: r["new"] for r in doc["rows"]} == {"ae-a": False, "ae-new": True}
    assert doc["since_baseline"]["new"] == 1
    assert doc["since_baseline"]["cleared"] == ["ae-gone"]
    html = _html(idx, resolve("/exceptions"))
    assert "1 new · 1 cleared" in html and "Gone row" in html


def test_no_baseline_marks_nothing_new():
    idx = Index()
    idx.add_result(AdapterResult(name="fixture", status=AdapterStatus.OK,
                                 claim_class=ClaimClass.OBSERVATION, entities=(_row("ae-a"),)))
    idx.finalize()
    doc = render_json.payload(idx, resolve("/exceptions"))
    assert doc["since_baseline"] is None and doc["rows"][0]["new"] is None
    assert "no previous day" in _html(idx, resolve("/exceptions"))


@pytest.mark.parametrize("doc", [{"rows": []}, {"view": "landing", "rows": []}, []])
def test_an_unusable_baseline_is_a_failed_source(doc):
    base, result = baseline.read(SRC, NOW, reader=lambda s, k: json.dumps(doc).encode())
    assert base is None and result.status is AdapterStatus.FAILED
    assert result.name == "exceptions-baseline" and result.unavailable


def test_a_malformed_block_fails_the_strict_preflight():
    with pytest.raises(console_config.ConfigError):
        console_config.validate_config({"exceptions_baseline": {"bucket": "b", "key": "k",
                                                                "days_back": 0}})


def test_a_folded_job_failing_under_a_newer_run_is_neither_cleared_nor_new():
    """Codex review cef0b43561b005e1: the folded row's id is its NEWEST run, so
    comparing ids reported a persistently failing job as cleared and new."""
    yesterday = {"view": "exceptions", "snapshot_utc": "2026-10-31T21:24:39Z", "rows": [
        {"id": "job-x@old", "kind": "run", "state": "FAILED", "label": "Job X"},
        {"id": "job-y@old", "kind": "run", "state": "FAILED", "label": "Job Y"}]}
    base = baseline.from_snapshot(yesterday)
    today = {"job-x@new": "run|job-x|FAILED", "job-y@new": "run|job-y|DEGRADED"}
    delta = baseline.compare(base, list(today), today)
    assert delta["new"] == ["job-y@new"]          # a different fact: now DEGRADED
    assert delta["cleared"] == ["job-y@old"]
    assert delta["cleared_labels"] == {"job-y@old": "Job Y"}
    assert baseline.row_key("job-x@new", "run", "FAILED") == "run|job-x|FAILED"
    assert baseline.row_key("ae-a", "component", "FAILED") == "ae-a"


def _unit_run_index(invocation, doc):
    """A systemd unit and its run the way `local_units` emits them: the run's
    id is the bare InvocationID, tied to its unit only by `belongs-to`."""
    from console.model.entity import Edge

    idx = Index()
    # The unit's own state is read later than the run, so the run is not a
    # component echo and lists in its own right.
    unit = Entity(kind=Kind.COMPONENT, id="unit-x", state=State.FAILED,
                  provenance=Provenance("systemd", "2026-11-01T11:00:00+00:00"))
    run = Entity(kind=Kind.RUN, id=invocation, state=State.FAILED,
                 provenance=Provenance("systemd", "2026-11-01T10:00:00+00:00"))
    idx.add_result(AdapterResult(
        name="local_units", status=AdapterStatus.OK, claim_class=ClaimClass.OBSERVATION,
        entities=(unit, run),
        edges=(Edge(source=invocation, rel="belongs-to", target="unit-x"),)))
    base, result = baseline.read(SRC, NOW, reader=lambda s, k: json.dumps(doc).encode())
    idx.add_result(result)
    idx.set_exceptions_baseline(base)
    idx.finalize()
    return idx


def test_a_unit_failing_under_a_newer_invocation_is_neither_cleared_nor_new():
    """Codex 10fe7de16b3c53ab: a systemd run's id has no `@`, so the baseline
    compared it by id and a unit still failing under a newer InvocationID read
    cleared-and-new. The baseline uses the folding logic's job identity, and
    the published row carries it so the next day's snapshot can match it."""
    first = _unit_run_index("inv-old", {"view": "exceptions", "snapshot_utc": "x", "rows": []})
    snap = render_json.payload(first, resolve("/exceptions"))
    assert {r["id"]: r["job"] for r in snap["rows"]}["inv-old"] == "unit-x"
    snap["snapshot_utc"] = "2026-10-31T21:24:39Z"
    idx = _unit_run_index("inv-new", snap)
    doc = render_json.payload(idx, resolve("/exceptions"))
    assert {r["id"]: r["new"] for r in doc["rows"]}["inv-new"] is False
    assert doc["since_baseline"]["cleared"] == []
    assert baseline.row_key("inv-new", "run", "FAILED", "unit-x") == "run|unit-x|FAILED"


def test_a_snapshot_from_before_the_job_key_does_not_churn_unchanged_runs():
    """Codex 8262684956a0132f: a snapshot written before rows carried `job`
    keys a bare-id run by its id, while today keys the same run by its job,
    so an unchanged run read as cleared-and-new on the first day after the
    change. The same id on both days is the same row."""
    old_snapshot = {"id": "inv-same", "kind": "run", "state": "FAILED", "label": "Unit X"}
    base = baseline.from_snapshot({"view": "exceptions", "snapshot_utc": "x",
                                   "rows": [old_snapshot]})
    today = {"inv-same": baseline.row_key("inv-same", "run", "FAILED", "unit-x")}
    delta = baseline.compare(base, list(today), today)
    assert delta["new"] == [] and delta["cleared"] == []
