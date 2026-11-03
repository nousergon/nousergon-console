"""Chart annotations (alpha-engine-config-I12784): a page chart marks events
other adapters already mint (deploys, promotions, incidents, kill-switch
events) on its own dates, and lists every mark in text under it.

A mark sits on the last point on or before its event's day; an event outside
the chart's dates, or without a date, is counted and never pinned to an edge or
dropped silently."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from console.index import pages
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import json as render_json
from console.server.app import _html
from console.server.router import resolve

NOW = datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)

#: Trading days only: 2026-10-24/25 (a weekend) carry no point.
DAYS = ["2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27",
        "2026-10-28"]


def _event(id_, kind, at, title, **facets):
    return Entity(kind=Kind.SIGNAL, id=id_, state=State.HEALTHY,
                  provenance=Provenance("s3://b/e.json", "2026-11-01T00:00:00+00:00"),
                  facets={"annot": kind, **facets},
                  detail={"fields": {"at": {"value": at, "render": "text"},
                                     "title": {"value": title, "render": "text"}},
                          "label": f"label of {id_}"})


ROWS = (
    Entity(kind=Kind.SIGNAL, id="perf:summary", state=State.HEALTHY,
           provenance=Provenance("s3://b/perf.json", "2026-10-28T02:00:00+00:00"),
           facets={"pane": "performance", "perf": "summary"},
           detail={"fields": {"underwater_pct": {
               "value": [{"date": d, "value": -float(i)} for i, d in enumerate(DAYS)],
               "unit": "%", "render": "timeseries"}}}),
    # A Saturday promotion lands on Friday's bar.
    _event("champ:m", "promotion", "2026-10-24T11:54:23Z", "m: v3meta_stack"),
    _event("champ:r", "promotion", "2026-10-21", "r: sleeve_top10"),
    # Before the first point and after the last: outside, counted.
    _event("champ:u", "promotion", "2026-10-05T21:47:34Z", "u: attractiveness"),
    _event("inc:1", "incident", "2026-10-31", "SEV1 after the series", sev="SEV1"),
    _event("inc:2", "incident", "2026-10-27", "SEV1 inside", sev="SEV1"),
    _event("inc:3", "incident", "2026-10-27", "SEV3 not selected", sev="SEV3"),
    # No date at all: counted as undated.
    _event("inc:4", "incident", None, "SEV1 undated", sev="SEV1"),
)


def _decl(annotations, range_="all"):
    return {"performance": {
        "title": "Performance", "question": "How has the portfolio done?",
        "range": range_,
        "sections": [{
            "type": "chart", "title": "Underwater", "fields": ["underwater_pct"],
            "select": {"kind": "signal", "facets": {"perf": "summary"}},
            **({"annotations": annotations} if annotations is not None else {}),
        }]}}


PROMOTIONS = {"label": "promotion", "at": "at", "text": "title",
              "select": {"kind": "signal", "facets": {"annot": "promotion"}}}
SEV1 = {"label": "incident", "at": "at",
        "select": {"facets": {"annot": "incident", "sev": "SEV1"}}}


def _index(decl):
    idx = Index()
    idx.add_result(AdapterResult(name="fixture", status=AdapterStatus.OK,
                                 claim_class=ClaimClass.OBSERVATION, entities=ROWS))
    pages.attach(idx, pages.parse(decl))
    idx.finalize()
    return idx


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    evaluate = pages.evaluate

    def at_now(index, p, params=None, now=None):
        return evaluate(index, p, params, now=now or NOW)

    monkeypatch.setattr(pages, "evaluate", at_now)


def _chart(decl, query=""):
    return render_json.payload(_index(decl), resolve("/page/performance", query)
                               )["page"]["sections"][0]


def test_marks_sit_on_the_last_point_on_or_before_the_event():
    sec = _chart(_decl([PROMOTIONS, SEV1]))
    marks = [(m["label"], m["day"], m["point"], m["text"]) for m in sec["annotations"]]
    assert marks == [
        ("promotion", "2026-10-21", "2026-10-21", "r: sleeve_top10"),
        ("promotion", "2026-10-24", "2026-10-23", "m: v3meta_stack"),
        ("incident", "2026-10-27", "2026-10-27", "label of inc:2"),
    ]
    assert [m["slot"] for m in sec["annotations"]] == [0, 2, 4]


def test_events_outside_the_dates_or_undated_are_counted_not_dropped():
    sec = _chart(_decl([PROMOTIONS, SEV1]))
    assert sec["annotations_outside"] == 2  # champ:u before, inc:1 after
    assert sec["annotations_undated"] == 1  # inc:4
    html = _html(_index(_decl([PROMOTIONS, SEV1])), resolve("/page/performance", ""))
    assert "2 outside the chart&#x27;s dates" in html
    assert "1 without a date" in html


def test_range_narrows_the_marks_with_the_points():
    sec = _chart(_decl([PROMOTIONS, SEV1], range_="7d"), "range=7d")
    # 7d before 2026-11-03 06:00Z keeps 10-27 (a bare date is a whole day)
    # and 10-28; only the 10-27 incident is still inside the chart.
    assert [p[0] for p in sec["points"]] == ["2026-10-27", "2026-10-28"]
    assert [m["id"] for m in sec["annotations"]] == ["inc:2"]
    assert sec["annotations_outside"] == 4


def test_html_draws_one_line_per_mark_and_lists_each_in_text():
    html = _html(_index(_decl([PROMOTIONS, SEV1])), resolve("/page/performance", ""))
    assert html.count('class="ts-mark"') == 3
    assert "3 event marks" in html
    assert '<time datetime="2026-10-24">2026-10-24</time> promotion:' in html
    assert ">m: v3meta_stack</a>" in html
    # The mark's own title names it too, so hovering the line says what it is.
    assert "<title>promotion 2026-10-24T11:54:23Z: m: v3meta_stack</title>" in html


def test_a_chart_without_annotations_is_unchanged():
    sec = _chart(_decl(None))
    assert "annotations" not in sec
    html = _html(_index(_decl(None)), resolve("/page/performance", ""))
    assert "ts-mark" not in html and "chart-marks" not in html


def test_no_event_inside_the_dates_says_so():
    only_outside = {"label": "promotion", "at": "at",
                    "select": {"facets": {"annot": "promotion"}}}
    html = _html(_index(_decl([dict(only_outside)], range_="7d")),
                 resolve("/page/performance", "range=7d"))
    assert "No event inside the chart's dates." in html


@pytest.mark.parametrize("annotations, message", [
    ([], "non-empty list"),
    ("x", "non-empty list"),
    (["x"], "must be a mapping"),
    ([{"at": "at", "select": {"facets": {"a": "b"}}}], "`label` is required"),
    ([{"label": "l", "select": {"facets": {"a": "b"}}}], "`at` is required"),
    ([{"label": "l", "at": "at", "select": {"facets": {}}}], "at least one facet"),
])
def test_a_bad_annotation_fails_the_build_by_name(annotations, message):
    with pytest.raises(pages.PagesConfigError, match=message):
        pages.parse(_decl(annotations))


def test_only_a_chart_takes_annotations():
    decl = {"p": {"title": "t", "question": "q", "sections": [
        {"type": "table", "title": "x", "select": {"facets": {"a": "b"}},
         "annotations": [SEV1]}]}}
    with pytest.raises(pages.PagesConfigError, match="only a chart takes `annotations`"):
        pages.parse(decl)
