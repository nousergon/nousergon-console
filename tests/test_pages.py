"""Declared pages (`/page/<id>`) — the declaration fails the build by name,
sections select rows other adapters mint, the URL carries range and
variables, tiles always show staleness and baseline, and HTML and JSON render
one evaluation (§3.8)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from console.index import pages, reports
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import json as render_json
from console.server.app import _html
from console.server.router import resolve

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def _spend(provider, state, mtd, at, priority=None):
    facets = {"pane": "cost", "meter": "spend", "provider": provider}
    if priority:
        facets["priority"] = priority
    return Entity(
        kind=Kind.RUN, id=f"spend:{provider}", state=state,
        provenance=Provenance("s3://b/spend.json", at, "https://e/spend"),
        facets=facets,
        detail={"fields": {
            "mtd": {"value": mtd, "unit": "USD", "render": "value", "of": "budget"},
            "budget": {"value": 100, "unit": "USD", "render": "value"},
            "pace": {"value": mtd / 100, "unit": "ratio", "baseline": 1.0,
                     "render": "ratio"},
        }})


ROWS = (
    _spend("aws", State.DEGRADED, 120, "2026-10-28T11:00:00+00:00", priority="P1"),
    _spend("neon", State.HEALTHY, 10, "2026-10-28T10:00:00+00:00"),
    _spend("openrouter", State.FAILED, 40, "2026-10-01T10:00:00+00:00", priority="P0"),
    _spend("vercel", State.UNREPORTED, 0, "2026-10-27T10:00:00+00:00"),
    Entity(kind=Kind.RUN, id="cost-daily", state=State.HEALTHY,
           provenance=Provenance("s3://b/daily.json", "2026-10-28T11:30:00+00:00"),
           facets={"pane": "cost", "meter": "daily"},
           detail={"fields": {
               "s3": {"value": [{"date": f"2026-10-{d:02d}", "value": float(d)}
                                for d in range(1, 29)],
                      "unit": "USD", "render": "timeseries", "of": "s3_budget"},
               "s3_budget": {"value": 20, "unit": "USD", "render": "value"},
           }}),
)

DECL = {
    "cost": {
        "title": "Cost",
        "question": "Is each provider's spend on track inside its budget?",
        "range": "30d",
        "variables": ["provider"],
        "sections": [
            {"type": "tiles", "title": "Month to date", "tiles": [
                {"label": "AWS", "field": "mtd",
                 "select": {"kind": "run", "facets": {"meter": "spend", "provider": "aws"}}},
                {"label": "AWS pace", "field": "pace",
                 "select": {"facets": {"meter": "spend", "provider": "aws"}}},
                {"label": "GCP", "field": "mtd",
                 "select": {"facets": {"meter": "spend", "provider": "gcp"}}},
            ]},
            {"type": "exceptions", "title": "What is wrong",
             "select": {"facets": {"pane": "cost"}}},
            {"type": "table", "title": "Spend", "fields": ["mtd", "pace"],
             "sort": "-mtd", "select": {"facets": {"meter": "spend"}}},
            {"type": "chart", "title": "S3 daily", "fields": ["s3"],
             "select": {"facets": {"meter": "daily"}}},
        ],
    },
}


def _index(decl=DECL):
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


def _page(idx, query=""):
    return render_json.payload(idx, resolve("/page/cost", query))["page"]


# ------------------------------------------------------------- declaration --

@pytest.mark.parametrize("bad, message", [
    ({"Cost": {}}, "lowercase"),
    ({"cost": {"title": "t", "question": "q", "sections": []}}, "non-empty"),
    ({"cost": {"title": "t", "question": "q", "sections": [{"type": "gauge"}]}},
     "must be one of"),
    ({"cost": {"title": "t", "question": "q", "sections": [
        {"type": "table", "title": "x", "select": {"facets": {}}}]}}, "at least one facet"),
    ({"cost": {"title": "t", "question": "q", "sections": [
        {"type": "table", "title": "x", "select": {"kind": "nope", "facets": {"a": "b"}}}]}},
     "unknown `kind`"),
    ({"cost": {"title": "t", "question": "q", "sections": [
        {"type": "chart", "title": "x", "select": {"facets": {"a": "b"}}}]}},
     "exactly one timeseries"),
    ({"cost": {"title": "t", "question": "q", "sections": [
        {"type": "report", "feed": "missing"}]}}, "no `reports:` feed"),
    ({"cost": {"title": "t", "question": "q", "range": "2w", "sections": [
        {"type": "report", "feed": "daily"}]}}, "`range` must be"),
    ({"cost": {"title": "t", "sections": [{"type": "report", "feed": "daily"}]}},
     "`question` is required"),
])
def test_a_bad_declaration_fails_the_build_by_name(bad, message):
    with pytest.raises(pages.PagesConfigError, match=message):
        pages.parse(bad, feeds={"daily"})


def test_the_page_is_wired_into_the_build():
    from console.config import build_index

    idx = build_index({"adapters": [], "reports": {"feeds": [
        {"id": "daily", "title": "Daily", "facets": {"pane": "reports"}}]},
        "pages": {"ops": {"title": "Ops", "question": "q", "sections": [
            {"type": "report", "feed": "daily"}]}}})
    assert [p.id for p in pages.declared(idx)] == ["ops"]


# ------------------------------------------------------------- evaluation --

def test_tiles_carry_value_staleness_and_baseline():
    tiles = _page(_index())["sections"][0]["tiles"]
    aws, pace, gcp = tiles
    assert aws["text"] == "120" and aws["unit"] == "USD"
    assert aws["as_of"] == "2026-10-28T11:00:00+00:00"
    assert aws["baseline"] == {"declared": False, "value": None}
    assert pace["versus_baseline"] == "above"
    assert gcp["id"] is None and gcp["absent"] == "no row matches the selector"


def test_exceptions_are_p0_first_then_untriaged_then_by_priority():
    sec = _page(_index(), "range=all")["sections"][1]
    # openrouter is P0, vercel is untriaged, aws is P1; neon is healthy.
    assert [r["id"] for r in sec["rows"]] == ["spend:openrouter", "spend:vercel", "spend:aws"]


def test_range_narrows_rows_and_chart_points():
    default = _page(_index())
    table = default["sections"][2]
    assert default["range"]["value"] == "30d"
    assert [r["id"] for r in table["rows"]][0] == "spend:aws"  # sorted -mtd
    week = _page(_index(), "range=7d")
    assert "spend:openrouter" not in [r["id"] for r in week["sections"][2]["rows"]]
    assert week["sections"][2]["outside_range"] == 1
    chart = week["sections"][3]
    assert [p[0] for p in chart["points"]][0] == "2026-10-21"
    assert chart["reference"] == {"name": "s3_budget", "value": 20}


def test_a_variable_narrows_only_rows_that_carry_its_facet():
    page = _page(_index(), "provider=neon")
    assert [r["id"] for r in page["sections"][2]["rows"]] == ["spend:neon"]
    # The chart's row has no `provider` facet, so the variable leaves it alone.
    assert page["sections"][3]["points"]
    assert page["variables"] == [{"name": "provider", "value": "neon",
                                  "options": ["aws", "neon", "openrouter", "vercel"]}]


def test_an_unknown_range_falls_back_to_the_declared_default():
    assert _page(_index(), "range=forever")["range"]["value"] == "30d"


# ------------------------------------------------------- representations --

def test_html_and_json_render_the_same_evaluation():
    idx = _index()
    req = resolve("/page/cost", "range=7d&provider=aws")
    html = _html(idx, req)
    doc = render_json.payload(idx, req)
    assert doc["view"] == "page" and html.startswith("<!doctype html>")
    assert "Is each provider&#x27;s spend on track" in html
    assert '<option value="7d" selected>' in html
    assert '<option value="aws" selected>' in html
    for r in doc["page"]["sections"][2]["rows"]:
        assert r["id"] in html
    assert "no baseline" in html and "above baseline 1.0" in html
    assert 'class="tile state-DEGRADED"' in html
    assert "page-chart" in html


def test_an_undeclared_page_is_a_404_in_both_representations():
    from console.server.app import _resolves

    idx = _index()
    assert not _resolves(idx, "/page/nope", "")
    assert _resolves(idx, "/page/cost", "")


def test_a_report_section_shows_the_feeds_latest_report():
    idx = Index()
    idx.add_result(AdapterResult(
        name="r", status=AdapterStatus.OK, claim_class=ClaimClass.OBSERVATION,
        entities=(Entity(kind=Kind.RUN, id="report:2026-10-28", state=State.HEALTHY,
                         provenance=Provenance("s3://b/r.md", "2026-10-28T10:00:00+00:00"),
                         facets={"pane": "reports"},
                         detail={"document": {"text": "# Today\n\nAll fine.",
                                              "format": "markdown"}}),)))
    feeds = reports.parse({"feeds": [{"id": "daily", "title": "Daily",
                                      "facets": {"pane": "reports"}}]})
    reports.attach(idx, feeds)
    pages.attach(idx, pages.parse({"cost": {"title": "Cost", "question": "q", "sections": [
        {"type": "report", "feed": "daily"}]}}, feeds={"daily"}))
    idx.finalize()
    sec = _page(idx)["sections"][0]
    assert sec["latest"]["id"] == "report:2026-10-28" and sec["url"] == "/reports/daily"
    assert "All fine." in _html(idx, resolve("/page/cost"))


def test_declared_pages_are_listed_on_the_dashboards_index():
    idx = _index()
    req = resolve("/dashboards")
    assert render_json.payload(idx, req)["pages"][0]["url"] == "/page/cost"
    assert '<a href="/page/cost">Cost</a>' in _html(idx, req)


# ------------------------------------------------------------ /exceptions --

def test_the_exceptions_page_lists_p0_then_untriaged_then_by_priority():
    idx = _index()
    req = resolve("/exceptions")
    doc = render_json.payload(idx, req)
    assert doc["view"] == "exceptions"
    assert [(r["id"], r["priority"]) for r in doc["rows"]] == [
        ("spend:openrouter", "P0"), ("spend:vercel", "untriaged"), ("spend:aws", "P1")]
    html = _html(idx, req)
    assert html.index('id="tier-P0"') < html.index('id="tier-untriaged"') < html.index('id="tier-P1"')
    assert "spend:neon" not in html


def test_the_landing_view_links_to_the_exceptions_page():
    assert 'href="/exceptions"' in _html(_index(), resolve("/"))
