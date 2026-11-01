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
