"""A GRID section: one row per `y` value, one column per `x` value, each cell
the row's own value (Brian 2026-11-13: the old dashboard's target-weight-by-
day grid, "I just need to know what are the buys and sells every day").

A cell no row fills is a dash, never zero; rows sort by the newest column;
a page variable on the grid's own `x` facet marks a column rather than
filtering the grid to one day."""
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

NOW = datetime(2026, 11, 13, 18, 0, tzinfo=timezone.utc)


def _held(day, ticker, target):
    return Entity(kind=Kind.DECISION, id=f"book:{day}:{ticker}", state=State.HEALTHY,
                  provenance=Provenance(f"s3://b/{day}.json", f"{day}T13:00:00+00:00"),
                  facets={"crucible_trading": "order-book", "trading_day": day},
                  detail={"fields": {
                      "trading_day": {"value": day, "render": "text"},
                      "ticker": {"value": ticker, "render": "text"},
                      "target_weight_pct": {"value": target, "unit": "pct", "render": "value"}}})


ROWS = (_held("2026-11-10", "AAA", 5.0), _held("2026-11-10", "BBB", 8.0),
        _held("2026-11-11", "AAA", 6.0), _held("2026-11-11", "BBB", 4.0),
        _held("2026-11-12", "AAA", 7.5), _held("2026-11-12", "CCC", 3.0))

GRID = dict(type="grid", title="Target weight by day", x="trading_day", y="ticker",
            fields=["target_weight_pct"],
            select={"kind": "decision", "facets": {"crucible_trading": "order-book"}})


def _decl(**grid):
    return {"order-book": {"title": "Order book", "question": "What did it buy and sell?",
                           "variables": ["trading_day"], "default_newest": ["trading_day"],
                           "sections": [{**GRID, **grid}]}}


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


def _section(decl, query=""):
    return render_json.payload(_index(decl), resolve("/page/order-book", query))["page"]["sections"][0]


def test_rows_by_name_columns_by_day_sorted_by_the_newest_column():
    sec = _section(_decl())
    assert sec["columns"] == ["2026-11-10", "2026-11-11", "2026-11-12"]
    assert [r["label"] for r in sec["rows"]] == ["AAA", "CCC", "BBB"]
    cells = {r["label"]: [c["value"] for c in r["cells"]] for r in sec["rows"]}
    # BBB left the book on 11-12 and CCC entered: an empty cell, never zero.
    assert cells["BBB"] == [8.0, 4.0, None]
    assert cells["CCC"] == [None, None, 3.0]
    assert sec["unit"] == "pct"


def test_the_day_picker_marks_a_column_instead_of_filtering_the_grid():
    sec = _section(_decl())
    assert sec["marked"] == "2026-11-12"  # default_newest
    picked = _section(_decl(), "trading_day=2026-11-11")
    assert picked["marked"] == "2026-11-11" and len(picked["columns"]) == 3


def test_columns_keeps_the_newest_days():
    sec = _section(_decl(columns=2))
    assert sec["columns"] == ["2026-11-11", "2026-11-12"]
    assert sec["columns_matched"] == 3


def test_limit_caps_rows():
    sec = _section(_decl(limit=1))
    assert [r["label"] for r in sec["rows"]] == ["AAA"]
    assert sec["matched"] == 3 and sec["shown"] == 1


def test_html_prints_every_value_and_a_dash_for_no_row():
    page = _html(_index(_decl()), resolve("/page/order-book", ""))
    assert '<table class="grid">' in page
    assert ">7.5</span>" in page and ">—</span>" in page
    assert 'class="grid-marked"' in page
    assert "a dash is no row that day, not zero" in page


@pytest.mark.parametrize("bad, why", [
    (dict(y=None), "takes `x`"),
    (dict(y="trading_day"), "must be different"),
    (dict(fields=["target_weight_pct", "ticker"]), "exactly one field"),
    (dict(sort="-target_weight_pct"), "does not apply to a grid"),
    (dict(columns=0), "`columns` must be"),
])
def test_bad_grids_fail_by_name(bad, why):
    with pytest.raises(pages.PagesConfigError, match=why):
        pages.parse(_decl(**bad))


def test_only_a_grid_takes_y():
    with pytest.raises(pages.PagesConfigError, match="only a grid takes"):
        pages.parse({"p": {"title": "P", "question": "q?", "sections": [{
            "type": "table", "title": "t", "y": "ticker",
            "select": {"facets": {"a": "b"}}}]}})
