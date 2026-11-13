"""Charts ACROSS rows (`x`): one point per matched row, for the optimizer views
the old dashboard drew (Brian 2026-11-12): a bar per name of a day's order
book (weight before and after), and a line per day of the book's own
summary (turnover, cash weight).

Every value is the row's own; a dated `x` is a time axis drawn as lines, any
other `x` is one bar group per row in `sort` order, and a row without the
`x` value is counted rather than placed."""
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

NOW = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)


def _name(ticker, prev, target, alpha, as_of="2026-11-12T13:00:00+00:00"):
    fields = {"ticker": {"value": ticker, "render": "text"},
              "previous_weight_pct": {"value": prev, "unit": "pct", "render": "value"},
              "target_weight_pct": {"value": target, "unit": "pct", "render": "value"},
              "alpha": {"value": alpha, "render": "ratio"}}
    if ticker is None:
        del fields["ticker"]
    return Entity(kind=Kind.DECISION, id=f"book:{ticker}", state=State.HEALTHY,
                  provenance=Provenance("s3://b/book.json", as_of),
                  facets={"crucible_trading": "order-book"}, detail={"fields": fields})


def _day(day, turnover, cash):
    return Entity(kind=Kind.RUN, id=f"day:{day}", state=State.HEALTHY,
                  provenance=Provenance("s3://b/day.json", f"{day}T13:00:00+00:00"),
                  facets={"crucible_trading": "order-book-day"},
                  detail={"fields": {"trading_day": {"value": day, "render": "text"},
                                     "turnover_one_way": {"value": turnover, "render": "ratio"},
                                     "cash_weight": {"value": cash, "render": "ratio"}}})


ROWS = (_name("AAA", 4.0, 6.5, 0.8), _name("BBB", 5.0, 0.0, -0.2),
        _name("CCC", 0.0, 3.0, 0.5), _name(None, 1.0, 1.0, 0.1),
        _day("2026-11-11", 0.04, 0.02), _day("2026-11-10", 0.09, 0.03),
        _day("2026-11-12", None, 0.02))


def _decl(**chart):
    return {"optimizer": {"title": "Optimizer", "question": "What did it move?",
                          "sections": [{"type": "chart", "title": "Book", **chart}]}}


BARS = dict(x="ticker", fields=["previous_weight_pct", "target_weight_pct"],
            sort="-target_weight_pct", limit=2,
            select={"kind": "decision", "facets": {"crucible_trading": "order-book"}})
LINES = dict(x="trading_day", fields=["turnover_one_way", "cash_weight"],
             select={"kind": "run", "facets": {"crucible_trading": "order-book-day"}})


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


def _section(decl):
    return render_json.payload(_index(decl), resolve("/page/optimizer", ""))["page"]["sections"][0]


def test_a_category_x_is_one_bar_group_per_row_in_sort_order():
    sec = _section(_decl(**BARS))
    assert sec["layout"] == "bars"
    assert sec["labels"] == ["AAA", "CCC"]  # largest target first, limit 2
    assert {s["field"]: s["values"] for s in sec["series"]} == {
        "previous_weight_pct": [4.0, 0.0], "target_weight_pct": [6.5, 3.0]}
    assert sec["unit"] == "pct"
    # The row without a ticker is counted, never placed.
    assert sec["matched"] == 4 and sec["shown"] == 2 and sec["unplaced"] == 1


def test_a_dated_x_is_a_time_axis_in_date_order_with_gaps():
    sec = _section(_decl(**LINES))
    assert sec["layout"] == "lines"
    assert sec["labels"] == ["2026-11-10", "2026-11-11", "2026-11-12"]
    values = {s["field"]: s["values"] for s in sec["series"]}
    assert values["turnover_one_way"] == [0.09, 0.04, None]  # a gap, never zero
    assert values["cash_weight"] == [0.03, 0.02, 0.02]


def test_bars_render_each_value_in_text_and_link_each_row():
    page = _html(_index(_decl(**BARS)), resolve("/page/optimizer", ""))
    assert '<ol class="bars"' in page
    assert "target_weight_pct </span>6.5 pct" in page and "previous_weight_pct </span>0 pct" in page
    assert 'class="bar-label" href="/decision/book:AAA' in page
    assert "2 of 4 rows · 1 without ticker" in page


def test_lines_reuse_the_overlay_drawing():
    page = _html(_index(_decl(**LINES)), resolve("/page/optimizer", ""))
    assert "ts-overlay" in page and "turnover_one_way 0.04 at 2026-11-11" in page


def test_mixed_units_are_refused_rather_than_drawn_on_one_axis():
    sec = _section(_decl(**{**BARS, "fields": ["target_weight_pct", "alpha"]}))
    assert "different units" in sec["absent"]


@pytest.mark.parametrize("bad, why", [
    (dict(type="table", x="ticker"), "only a chart or a grid takes `x`"),
    (dict(x="ticker", fields=["ticker"]), "also a series"),
    (dict(sort="-alpha", fields=["alpha"]), "takes `sort`"),
])
def test_bad_declarations_fail_by_name(bad, why):
    sec = {"type": "chart", "title": "t", "fields": ["alpha"],
           "select": {"kind": "decision", "facets": {"a": "b"}}, **bad}
    with pytest.raises(pages.PagesConfigError, match=why):
        pages.parse({"p": {"title": "P", "question": "q?", "sections": [sec]}})
