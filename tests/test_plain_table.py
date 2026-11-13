"""A PLAIN table (Brian 2026-11-13: the order book "is so bad, I cannot read
it"): declared labels as headers, values written for a reader, the first
column linking the row, no provenance columns; and a table sorted by a field
it does not show still sorts."""
from __future__ import annotations

import pytest

from console.index import pages
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.server.app import _html
from console.server.router import resolve


def _name(ticker, pos, weight, trade, usd):
    return Entity(kind=Kind.DECISION, id=f"book:{ticker}", state=State.HEALTHY,
                  provenance=Provenance("s3://b/book.json", "2026-11-13T13:00:00+00:00"),
                  facets={"crucible_trading": "order-book"},
                  detail={"fields": {
                      "ticker": {"value": ticker, "render": "text"},
                      "book_position": {"value": pos, "render": "count", "unit": "rank"},
                      "target_weight_pct": {"value": weight, "unit": "pct", "render": "value"},
                      "trade_shares": {"value": trade, "unit": "shares", "render": "count"},
                      "trade_notional_usd": {"value": usd, "unit": "USD"}}})


ROWS = (_name("BBB", 2, 4.5, -88, -31518.21), _name("AAA", 1, 12.03, 891, 45730.11),
        _name("CCC", 3, 1.0, None, None))

TABLE = {"type": "table", "title": "Bought", "plain": True, "sort": "book_position",
         "select": {"kind": "decision", "facets": {"crucible_trading": "order-book"}},
         "fields": [{"field": "ticker", "label": "Name"},
                    {"field": "trade_shares", "label": "Shares", "signed": True},
                    {"field": "trade_notional_usd", "label": "Dollars", "signed": True},
                    "target_weight_pct"]}


def _page(**over):
    idx = Index()
    idx.add_result(AdapterResult(name="fixture", status=AdapterStatus.OK,
                                 claim_class=ClaimClass.OBSERVATION, entities=ROWS))
    pages.attach(idx, pages.parse({"ob": {"title": "OB", "question": "q?",
                                          "sections": [{**TABLE, **over}]}}))
    idx.finalize()
    return _html(idx, resolve("/page/ob", ""))


def test_labels_head_the_columns_and_no_provenance_columns_show():
    page = _page()
    table = page[page.index('<table class="plain">'):]
    assert "<th>Name</th>" in table and "<th>target weight pct</th>" in table
    for gone in ("<th>id</th>", "<th>state</th>", "<th>evidence</th>"):
        assert gone not in table


def test_values_are_written_for_a_reader_with_signs():
    page = _page()
    assert ">+891<" in page and ">+$45,730<" in page and ">12.03%<" in page
    assert ">-88<" in page and ">-$31,518<" in page
    assert 'class="num num-neg"' in page
    assert ">—<" in page  # a missing value is a dash


def test_a_table_sorts_by_a_field_it_does_not_show():
    page = _page()
    assert page.index('href="/decision/book:AAA') < page.index('href="/decision/book:BBB') \
        < page.index('href="/decision/book:CCC')


@pytest.mark.parametrize("bad, why", [
    (dict(type="chart", fields=["target_weight_pct"]), "only a table can be `plain`"),
    (dict(fields=[{"field": "ticker", "colour": "red"}]), "unknown key"),
    (dict(fields=["ticker", {"field": "ticker"}]), "twice"),
    (dict(plain="yes"), "true or false"),
])
def test_bad_plain_tables_fail_by_name(bad, why):
    with pytest.raises(pages.PagesConfigError, match=why):
        pages.parse({"ob": {"title": "OB", "question": "q?", "sections": [{**TABLE, **bad}]}})
