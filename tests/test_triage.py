"""The triage overlay: each red row shows the priority of the issue tracking it."""
from __future__ import annotations

import json

import pytest

from console import config as console_config
from console.index import triage
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import json as render_json
from console.server.app import _html
from console.server.router import resolve

SRC = triage.TriageSource(bucket="b", key="ops/daily-summaries/console-triage.json")


def _row(rid, state, priority=None):
    facets = {"owner": "data"}
    if priority:
        facets["priority"] = priority
    return Entity(kind=Kind.COMPONENT, id=rid, state=state,
                  provenance=Provenance("s3://b/x.json", "2026-10-30T10:00:00+00:00"),
                  facets=facets)


def _index(doc):
    idx = Index()
    idx.add_result(AdapterResult(name="fixture", status=AdapterStatus.OK,
                                 claim_class=ClaimClass.OBSERVATION, entities=(
                                     _row("ae-a", State.FAILED),
                                     _row("ae-b", State.DEGRADED),
                                     _row("ae-c", State.STALLED, priority="P3"),
                                     _row("ae-d", State.FAILED))))
    marks, result = triage.read(SRC, "2026-10-30T12:00:00Z",
                                reader=lambda _s: json.dumps(doc).encode())
    idx.add_result(result)
    idx.set_triage(marks)
    idx.finalize()
    return idx


DOC = {"generated_utc": "2026-10-30T11:00:00Z", "rows": {
    "ae-a": {"issue": "alpha-engine-config-I1", "url": "https://gh/1", "priority": "P2"},
    "ae-b": {"issue": "alpha-engine-config-I2", "url": "https://gh/2", "priority": "p0"},
    "ae-c": {"issue": "alpha-engine-config-I3", "priority": "P1"},
    "gone": {"issue": "alpha-engine-config-I4", "priority": "P1"},
}}


def test_rows_take_their_issues_priority_and_sort_p0_first():
    idx = _index(DOC)
    doc = render_json.payload(idx, resolve("/exceptions"))
    assert [(r["id"], r["priority"], r["tracked_by"]) for r in doc["rows"]] == [
        ("ae-b", "P0", "alpha-engine-config-I2"),
        ("ae-d", "untriaged", None),
        ("ae-a", "P2", "alpha-engine-config-I1"),
        # Its own source ranked it P3; the overlay links the issue, never re-ranks.
        ("ae-c", "P3", "alpha-engine-config-I3"),
    ]
    assert idx.triage_counts() == {"entries": 4, "matched": 3}


def test_the_exceptions_page_links_the_tracking_issue_or_says_none():
    html = _html(_index(DOC), resolve("/exceptions"))
    assert '<a href="https://gh/1">alpha-engine-config-I1</a>' in html
    assert "no issue" in html


def test_the_overlay_never_changes_a_rows_state():
    idx = _index(DOC)
    assert idx.entity("ae-a").state_value == "FAILED"


@pytest.mark.parametrize("doc", [
    {"rows": []},
    {"rows": {"ae-a": {"priority": "P1"}}},
    {"rows": {"ae-a": {"issue": "x-I1", "priority": "high"}}},
])
def test_an_unusable_map_is_a_failed_source_not_silence(doc):
    marks, result = triage.read(SRC, "now", reader=lambda _s: json.dumps(doc).encode())
    assert marks == {}
    assert result.status is AdapterStatus.FAILED
    assert result.name == "triage-overlay" and result.unavailable


def test_an_unreadable_object_is_a_failed_source():
    def boom(_s):
        raise RuntimeError("NoSuchKey")

    marks, result = triage.read(SRC, "now", reader=boom)
    assert marks == {} and result.status is AdapterStatus.FAILED
    assert "NoSuchKey" in result.unavailable[0]


def test_a_malformed_triage_block_fails_the_strict_preflight():
    with pytest.raises(console_config.ConfigError):
        console_config.validate_config({"triage": {"bucket": "b"}})
    assert triage.parse(None) is None
