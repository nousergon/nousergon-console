"""Overlay charts (alpha-engine-config-I12798): a page chart that declares
several timeseries fields of ONE row draws each as a line on one axis — a
portfolio's growth beside its benchmarks'.

The console computes nothing: every value is the row's own. The x axis is the
union of the series' labels, a series with no point at a label is a gap there
(never zero, never bridged), a declared field the row lacks is named as not
drawn, and series in different units are refused rather than drawn on one
misleading axis."""
from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime, timezone

import pytest

from console.index import pages
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import html as render_html
from console.render import json as render_json
from console.server.app import _html
from console.server.router import resolve

NOW = datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc)


def _series(pairs):
    return {"value": dict(pairs), "unit": "%", "render": "timeseries"}


#: Crucible beside SPY and QQQ, as `{date: cumulative %}`. QQQ has no point on
#: 10-29 (a gap) and SPY's map arrives out of date order.
FIELDS = {
    "growth_portfolio_pct": _series([("2026-10-28", 0.0), ("2026-10-29", -0.28),
                                     ("2026-10-30", -3.32), ("2026-11-02", -3.41)]),
    "growth_spy_pct": _series([("2026-11-02", -2.24), ("2026-10-28", 0.0),
                               ("2026-10-29", 0.12), ("2026-10-30", -1.66)]),
    "growth_qqq_pct": _series([("2026-10-28", 0.0), ("2026-10-30", -2.9),
                               ("2026-11-02", -3.1)]),
    "nav_usd": {"value": {"2026-10-28": 1.0e6}, "unit": "USD", "render": "timeseries"},
    "as_of": {"value": "2026-11-02", "render": "text"},
}


def _row(fields=None):
    return Entity(kind=Kind.SIGNAL, id="perf:summary", state=State.HEALTHY,
                  provenance=Provenance("s3://b/perf.json", "2026-11-04T08:21:23+00:00"),
                  facets={"pane": "performance", "perf": "summary"},
                  detail={"fields": fields if fields is not None else FIELDS})


def _promotion():
    return Entity(kind=Kind.SIGNAL, id="champ:m", state=State.HEALTHY,
                  provenance=Provenance("s3://b/e.json", "2026-11-01T00:00:00+00:00"),
                  facets={"annot": "promotion"},
                  detail={"fields": {"at": {"value": "2026-10-31T11:54:23Z", "render": "text"}},
                          "label": "m: v3meta_stack"})


def _decl(fields, range_="all", annotations=None):
    sec = {"type": "chart", "title": "Growth since inception", "fields": fields,
           "select": {"kind": "signal", "facets": {"perf": "summary"}}}
    if annotations:
        sec["annotations"] = annotations
    return {"performance": {"title": "Performance", "question": "How has it done?",
                            "range": range_, "sections": [sec]}}


def _index(decl, rows=None):
    idx = Index()
    idx.add_result(AdapterResult(name="fixture", status=AdapterStatus.OK,
                                 claim_class=ClaimClass.OBSERVATION,
                                 entities=rows if rows is not None else (_row(), _promotion())))
    pages.attach(idx, pages.parse(decl))
    idx.finalize()
    return idx


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    evaluate = pages.evaluate

    def at_now(index, p, params=None, now=None):
        return evaluate(index, p, params, now=now or NOW)

    monkeypatch.setattr(pages, "evaluate", at_now)


THREE = ["growth_portfolio_pct", "growth_spy_pct", "growth_qqq_pct"]


def _section(decl, query="", rows=None):
    return render_json.payload(_index(decl, rows), resolve("/page/performance", query)
                               )["page"]["sections"][0]


def _page(decl, query="", rows=None):
    return _html(_index(decl, rows), resolve("/page/performance", query))


def test_each_series_is_aligned_on_the_union_of_dates_in_date_order():
    sec = _section(_decl(THREE))
    assert sec["fields"] == THREE and "field" not in sec
    assert sec["labels"] == ["2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02"]
    assert sec["unit"] == "%"
    assert [s["field"] for s in sec["series"]] == THREE
    values = {s["field"]: s["values"] for s in sec["series"]}
    assert values["growth_portfolio_pct"] == [0.0, -0.28, -3.32, -3.41]
    # Out-of-order input is placed by date, not by position.
    assert values["growth_spy_pct"] == [0.0, 0.12, -1.66, -2.24]
    # No QQQ point on 10-29: a gap, never zero and never interpolated.
    assert values["growth_qqq_pct"] == [0.0, None, -2.9, -3.1]
    assert sec["missing"] == [] and sec["outside_range"] == 0


def test_a_single_field_chart_keeps_its_shape():
    sec = _section(_decl(["growth_portfolio_pct"]))
    assert sec["field"] == "growth_portfolio_pct" and "fields" not in sec
    assert [p[0] for p in sec["points"]][0] == "2026-10-28"


def test_a_field_the_row_lacks_is_named_and_the_rest_still_draw():
    sec = _section(_decl(["growth_portfolio_pct", "growth_iwm_pct", "as_of"]))
    assert [s["field"] for s in sec["series"]] == ["growth_portfolio_pct"]
    assert sec["missing"] == [
        {"field": "growth_iwm_pct", "why": "the newest row carries no such field"},
        {"field": "as_of", "why": "not a timeseries this console can read"},
    ]
    page = _page(_decl(["growth_portfolio_pct", "growth_iwm_pct"]))
    assert "Not drawn: growth_iwm_pct (the newest row carries no such field)" in page


def test_series_in_different_units_share_no_axis():
    sec = _section(_decl(["growth_portfolio_pct", "nav_usd"]))
    assert "different units" in sec["absent"] and "series" not in sec
    assert "different units" in _page(_decl(["growth_portfolio_pct", "nav_usd"]))


def test_no_series_at_all_is_absent_not_an_empty_chart():
    sec = _section(_decl(["x_pct", "y_pct"]))
    assert sec["absent"].startswith("the newest row carries none of the fields")


def test_no_row_is_absent():
    sec = _section(_decl(THREE), rows=())
    assert sec["absent"] == "no row matches the selector"


def test_range_narrows_every_series_together():
    sec = _section(_decl(THREE, range_="7d"), "range=7d")
    # 7d before 11-05 06:00Z is 10-29 06:00Z: 10-28 (a whole day, ending
    # 10-29 00:00Z) is out; 10-29 is partly inside and stays.
    assert sec["labels"] == ["2026-10-29", "2026-10-30", "2026-11-02"]
    assert sec["outside_range"] == 1
    values = {s["field"]: s["values"] for s in sec["series"]}
    assert values["growth_qqq_pct"] == [None, -2.9, -3.1]
    assert values["growth_spy_pct"] == [0.12, -1.66, -2.24]


def test_marks_sit_on_the_overlay_dates():
    promotions = [{"label": "promotion", "at": "at",
                   "select": {"kind": "signal", "facets": {"annot": "promotion"}}}]
    sec = _section(_decl(THREE, annotations=promotions))
    # A Saturday promotion lands on Friday 10-30.
    assert [(m["day"], m["point"], m["slot"]) for m in sec["annotations"]] == [
        ("2026-10-31", "2026-10-30", 2)]
    page = _page(_decl(THREE, annotations=promotions))
    assert page.count('class="ts-mark"') == 1 and "1 event mark" in page


def test_html_draws_one_coloured_line_per_series_with_its_latest_value_in_text():
    page = _page(_decl(THREE))
    for k in (1, 2, 3):
        assert page.count(f'<path class="ts-trace ts-trace-{k}"') == 1
    # Every series' latest value is printed beside its colour (§5.7).
    assert "growth_portfolio_pct -3.41 % at 2026-11-02" in page
    assert "growth_qqq_pct -3.1 % at 2026-11-02" in page
    assert '<ul class="ts-legend">' in page
    # Values cross zero, so a zero rule is drawn.
    assert 'class="ts-zero"' in page
    # Uncoloured as a verdict: no state colour reaches the chart.
    chart = page[page.index('<figure class="page-chart">'):page.index("</figure>")]
    assert "var(--bad)" not in chart and "var(--good)" not in chart


def test_a_gap_breaks_the_line_and_is_never_bridged():
    path = re.search(r'<path class="ts-trace ts-trace-3" d="([^"]+)"', _page(_decl(THREE)))
    # QQQ: a lone point on 10-28 (a short tick), then a run 10-30 → 11-02.
    assert path.group(1).count("M") == 2


def test_the_svg_carries_every_series_at_full_precision_for_the_live_layer():
    page = _page(_decl(THREE))
    m = re.search(r'<svg class="ts-chart ts-overlay" data-chart="([^"]*)"', page)
    data = json.loads(html_lib.unescape(m.group(1)))
    assert data["key"] == "+".join(THREE)
    assert data["unit"] == "%"
    assert data["labels"] == ["2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02"]
    assert [(s["label"], s["kind"]) for s in data["series"]] == [(f, "trace") for f in THREE]
    assert data["series"][2]["values"] == [0.0, None, -2.9, -3.1]


def test_an_empty_range_says_so_and_still_names_what_is_not_drawn():
    sec = {"type": "chart", "id": "x", "fields": ["a", "b"], "labels": [], "series": [],
           "missing": [{"field": "b", "why": "the newest row carries no such field"}],
           "url": "/x"}
    out = render_html._page_chart(None, sec)
    assert "no point inside the selected range" in out
    assert "Not drawn: b (the newest row carries no such field)" in out


@pytest.mark.parametrize("fields, message", [
    ([], "exactly one timeseries field, or 2..6"),
    ([f"f{i}" for i in range(7)], "exactly one timeseries field, or 2..6"),
    (["a", "a"], "names a series twice"),
])
def test_a_bad_overlay_fails_the_build_by_name(fields, message):
    with pytest.raises(pages.PagesConfigError, match=re.escape(message)):
        pages.parse(_decl(fields))


def test_every_trace_colour_is_a_token_in_both_schemes_and_in_the_layer():
    from pathlib import Path

    static = Path(render_html.__file__).resolve().parent.parent / "static"
    css = (static / "styles.css").read_text()
    script = (static / "charts.js").read_text()
    dark = css[css.index("@media (prefers-color-scheme: dark)"):]
    for k in range(1, pages.MAX_CHART_SERIES + 1):
        assert f"--chart-{k}:" in css and f"--chart-{k}:" in dark, k
        assert f".ts-trace-{k} {{ stroke: var(--chart-{k});" in css, k
        assert f'"chart-{k}"' in script, k
