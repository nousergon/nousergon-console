"""Home is summary-only (CN-4.3, amended 2026-10-28): one health tile per
declared area plus `unassigned`, then the exception counts, then headline
tiles. No row list, and no tile speaks for more than its own area."""
from __future__ import annotations

import pytest

from console.index import home, pages
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import json as render_json
from console.server.app import _html
from console.server.router import resolve


def _row(id_, kind, state, pane, at="2026-10-28T10:00:00+00:00"):
    return Entity(kind=kind, id=id_, state=state, provenance=Provenance("s", at),
                  facets={"pane": pane},
                  detail={"fields": {"mtd": {"value": 5, "unit": "USD", "render": "value"}}})


ROWS = (
    _row("exec", Kind.COMPONENT, State.FAILED, "trading", "2026-10-27T00:00:00+00:00"),
    _row("ledger", Kind.COMPONENT, State.HEALTHY, "trading"),
    _row("spend", Kind.RUN, State.HEALTHY, "cost"),
    _row("stray", Kind.COMPONENT, State.DEGRADED, "misc"),
)
DECL = {
    "areas": [
        {"id": "trading", "title": "Trading", "link": "/page/trading",
         "select": {"facets": {"pane": "trading"}}},
        {"id": "cost", "title": "Cost", "link": "/run?pane=cost",
         "select": [{"kind": "run", "facets": {"pane": "cost"}}]},
    ],
    "tiles": [{"label": "MTD spend", "field": "mtd",
               "select": {"facets": {"pane": "cost"}}}],
}


def _index(decl=DECL):
    idx = Index()
    idx.add_result(AdapterResult(name="f", status=AdapterStatus.OK,
                                 claim_class=ClaimClass.OBSERVATION, entities=ROWS))
    home.attach(idx, home.parse(decl))
    idx.finalize()
    return idx


@pytest.mark.parametrize("bad, message", [
    ("x", "must be a mapping"),
    ({"areas": [{"id": "a", "title": "A", "link": "/x"}]}, "`select`"),
    ({"areas": [{"id": "a", "title": "A", "link": "x", "select": {"facets": {"p": "q"}}}]},
     "console path"),
    ({"areas": [{"id": "unassigned", "title": "A", "link": "/x",
                 "select": {"facets": {"p": "q"}}}]}, "reserved"),
    ({"tiles": [{"label": "x", "select": {"facets": {"p": "q"}}}]}, "`field`"),
])
def test_a_bad_home_declaration_fails_by_name(bad, message):
    with pytest.raises(pages.PagesConfigError, match=message):
        home.parse(bad)


def test_each_area_says_its_own_state_and_an_unclaimed_row_is_never_dropped():
    doc = render_json.payload(_index(), resolve("/"))
    by_id = {a["id"]: a for a in doc["health"]}
    assert by_id["trading"]["worst"] == "FAILED"
    assert (by_id["trading"]["not_healthy"], by_id["trading"]["rows"]) == (1, 2)
    assert by_id["trading"]["stalest"]["id"] == "exec"
    assert by_id["cost"]["worst"] == "HEALTHY"
    assert by_id["cost"]["stalest"]["basis"] == "latest run"
    assert by_id["unassigned"]["worst"] == "DEGRADED" and by_id["unassigned"]["rows"] == 1
    assert doc["headline"][0]["text"] == "5"


def test_home_opens_with_health_then_counts_then_tiles_and_lists_no_rows():
    page = _html(_index(), resolve("/"))
    h = page.index("system health")
    assert h < page.index("<h2>exceptions</h2>") < page.index("<h2>headlines</h2>")
    assert 'href="/page/trading"' in page and 'href="/run?pane=cost"' in page
    assert "<table" not in page.split("<h2>headlines</h2>")[0]
    assert ">exec<" not in page and ">stray<" not in page
