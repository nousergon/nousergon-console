"""Home and /exceptions chart the not-healthy count day by day."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from console.index import baseline
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import json as render_json
from console.server.app import _html
from console.server.router import resolve

SRC = baseline.BaselineSource(bucket="b", key="k-{date}.json",
                              trend_key="ops/console/exceptions-trend.json")
NOW = datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)
TREND = {"updated_utc": "2026-11-02T14:07:45Z", "days": [
    {"date": "2026-11-01", "total": 151, "by_priority": {"P1": 7, "untriaged": 31}},
    {"date": "2026-10-31", "total": 148, "by_priority": {}},
    {"date": "2026-11-02", "total": 109, "by_priority": {"P1": 4}}]}


def _index(doc=TREND):
    idx = Index()
    idx.add_result(AdapterResult(
        name="fixture", status=AdapterStatus.OK, claim_class=ClaimClass.OBSERVATION,
        entities=(Entity(kind=Kind.COMPONENT, id="ae-a", state=State.FAILED,
                         provenance=Provenance("s3://b/x.json", "2026-11-02T10:00:00+00:00")),)))
    points, result = baseline.read_trend(SRC, NOW, reader=lambda s, k: json.dumps(doc).encode())
    idx.add_result(result)
    idx.set_exceptions_trend(points)
    idx.finalize()
    return idx, result


def test_days_sort_oldest_first_and_today_is_a_live_bar():
    idx, result = _index()
    assert result.status is AdapterStatus.OK
    assert [p.date for p in idx.exceptions_trend()] == ["2026-10-31", "2026-11-01", "2026-11-02"]
    for path in ("/", "/exceptions"):
        html = _html(idx, resolve(path))
        assert "2026-10-31 – now: 148 → 1 (-147)" in html
        assert 'class="ts-live"' in html
    doc = render_json.payload(idx, resolve("/exceptions"))
    assert [d["total"] for d in doc["trend"]] == [148, 151, 109]


def test_no_trend_key_draws_nothing_and_adds_no_source():
    points, result = baseline.read_trend(
        baseline.BaselineSource(bucket="b", key="k"), NOW, reader=lambda s, k: b"{}")
    assert points is None and result is None
    idx = Index()
    idx.finalize()
    assert "exception-trend" not in _html(idx, resolve("/"))


@pytest.mark.parametrize("doc", [{}, {"days": [{"date": "2026-11-01", "total": -1}]},
                                 {"days": [{"total": 3}]}, {"days": "x"}])
def test_an_unusable_trend_is_a_failed_source_and_no_chart(doc):
    idx, result = _index(doc)
    assert result.status is AdapterStatus.FAILED
    assert result.name == baseline.TREND_SOURCE_NAME
    assert "exception-trend" not in _html(idx, resolve("/exceptions"))


def test_only_the_last_thirty_days_are_drawn():
    doc = {"days": [{"date": f"2026-10-{d:02d}", "total": d} for d in range(1, 32)]}
    points = baseline.trend_points(doc)
    assert len(points) == baseline.TREND_DAYS and points[0].date == "2026-10-02"


def test_a_bad_trend_key_fails_the_preflight():
    with pytest.raises(ValueError):
        baseline.parse({"bucket": "b", "key": "k", "trend_key": ""})
    assert baseline.parse({"bucket": "b", "key": "k", "trend_key": "t.json"}).trend_key == "t.json"


def test_every_page_menu_links_the_exceptions_list():
    idx = Index()
    idx.finalize()
    for path in ("/", "/dashboards", "/exceptions"):
        html = _html(idx, resolve(path))
        nav = html[html.index('<nav class="site-nav">'):]
        nav = nav[:nav.index("</nav>")]
        assert '<a href="/exceptions">exceptions</a>' in nav, path
