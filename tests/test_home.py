"""Home is summary-only (CN-4.3, amended 2026-10-28): one health tile per
declared area plus `unassigned`, then the exception counts, then headline
tiles. No row list, and no tile speaks for more than its own area."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

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


def test_an_area_may_own_a_whole_kind_and_a_star_matches_any_value():
    idx = _index({"areas": [
        {"id": "svc", "title": "Services", "link": "/component",
         "select": [{"kind": "component"}]},
        {"id": "all-panes", "title": "Any pane", "link": "/run",
         "select": [{"facets": {"pane": "*"}}]}]})
    by_id = {a["id"]: a for a in render_json.payload(idx, resolve("/"))["health"]}
    # first area wins: the 3 components stay in svc, the cost run falls to all-panes
    assert by_id["svc"]["rows"] == 3 and by_id["all-panes"]["rows"] == 1
    assert by_id["unassigned"]["rows"] == 0


def test_a_page_selector_still_needs_a_facet():
    with pytest.raises(pages.PagesConfigError, match="at least one facet"):
        pages.parse({"p": {"title": "t", "question": "q", "sections": [
            {"type": "table", "title": "x", "select": {"kind": "run"}}]}})


def test_a_tile_reads_the_newest_row_that_carries_its_field_and_shows_its_whole():
    idx = Index()
    old = Entity(kind=Kind.DECISION, id="summary", state=State.HEALTHY,
                 provenance=Provenance("s", "2026-10-27T00:00:00+00:00"),
                 facets={"pane": "dc"},
                 detail={"fields": {"met": {"value": 7, "unit": "clauses", "render": "count",
                                            "of": "total"},
                                    "total": {"value": 9, "unit": "clauses", "render": "count"}}})
    new = Entity(kind=Kind.DECISION, id="ladder", state=State.HEALTHY,
                 provenance=Provenance("s", "2026-10-28T00:00:00+00:00"), facets={"pane": "dc"})
    idx.add_result(AdapterResult(name="f", status=AdapterStatus.OK,
                                 claim_class=ClaimClass.OBSERVATION, entities=(old, new)))
    home.attach(idx, home.parse({"tiles": [{"label": "met", "field": "met",
                                            "select": {"facets": {"pane": "dc"}}}]}))
    idx.finalize()
    tile = render_json.payload(idx, resolve("/"))["headline"][0]
    assert tile["id"] == "summary" and tile["of"]["text"] == "9"
    assert "of 9" in _html(idx, resolve("/"))


def test_a_tile_never_reads_failed_over_zero_exceptions():
    # Live 2026-10-29: "Trading and models FAILED · 0 not healthy of 210". The
    # word came from a row the exception list does not count (a superseded
    # run); the tile's word must agree with its own count.
    idx = _index()
    areas = {a["id"]: a for a in home.health(idx, [])}
    assert areas["trading"]["not_healthy"] == 0
    assert areas["trading"]["worst"] == "HEALTHY"
    empty = home.health(Index(), [])
    assert empty[-1]["worst"] is None


def test_the_first_declared_area_owns_a_row_so_tiles_partition_the_fleet():
    decl = {"areas": [
        {"id": "trading", "title": "T", "link": "/t", "select": {"facets": {"pane": "trading"}}},
        {"id": "all", "title": "All", "link": "/a", "select": {"kind": "component"}},
    ]}
    by_id = {a["id"]: a for a in home.health(_index(decl), [])}
    assert by_id["trading"]["rows"] == 2
    assert by_id["all"]["rows"] == 1          # only "stray"; trading's rows stay there
    assert by_id["unassigned"]["rows"] == 1   # the cost run


def test_home_groups_answer_one_question_each():
    """Brian 2026-11-04: home must answer what he asks. A group is a titled
    row of tiles, linked to its page; it renders after the exception counts."""
    decl = dict(DECL, groups=[{"title": "Cost this month", "link": "/run?pane=cost",
                               "tiles": [{"label": "spend", "field": "mtd",
                                          "select": {"facets": {"pane": "cost"}}}]}])
    page = _html(_index(decl), resolve("/"))
    assert '<h2><a href="/run?pane=cost">Cost this month</a></h2>' in page
    assert page.index("<h2>exceptions</h2>") < page.index("Cost this month")
    doc = render_json.payload(_index(decl), resolve("/"))
    assert doc["headline_groups"][0]["title"] == "Cost this month"
    assert doc["headline_groups"][0]["tiles"][0]["label"] == "spend"


@pytest.mark.parametrize("bad, message", [
    ({"groups": [{"title": "x"}]}, "at least one tile"),
    ({"groups": [{"title": "x", "link": "http://y", "tiles": [
        {"label": "a", "field": "b", "select": {"facets": {"p": "q"}}}]}]}, "console path"),
])
def test_a_bad_group_fails_by_name(bad, message):
    with pytest.raises(home.HomeConfigError, match=message):
        home.parse(bad)


def test_home_is_plain_and_the_internals_are_on_health():
    idx = _index()
    page = _html(idx, resolve("/"))
    assert "transparency gap" not in page and "reachability" not in page
    assert '<a href="/health">system health details</a>' in page
    health = _html(idx, resolve("/health"))
    assert "transparency gap" in health and "index reachability" in health


def _note(id_, rank, at, sev="warning"):
    return Entity(kind=Kind.SIGNAL, id=id_, state=State.HEALTHY,
                  provenance=Provenance("s", at),
                  facets={"pane": "console-review", "record": "insight"},
                  detail={"label": f"insight {id_}", "fields": {
                      "rank": {"value": rank, "render": "count"},
                      "severity": {"value": sev, "render": "text"},
                      "fix": {"value": f"fix {id_}", "render": "text"},
                      "your_action": {"value": "nothing", "render": "text"},
                      "page": {"value": "/page/incidents", "render": "text"}}})


NOTES = dict(DECL, notes={
    "title": "Things to know", "select": {"facets": {"pane": "console-review"}},
    "fields": [{"field": "fix", "label": "Fix"}, {"field": "your_action", "label": "You"}],
    "severity_field": "severity", "link_field": "page", "sort": "rank", "limit": 2})


def _notes_index(rows):
    idx = Index()
    idx.add_result(AdapterResult(name="f", status=AdapterStatus.OK,
                                 claim_class=ClaimClass.OBSERVATION, entities=ROWS + tuple(rows)))
    home.attach(idx, home.parse(NOTES))
    idx.finalize()
    return idx


def test_notes_show_the_newest_batch_sorted_and_capped_above_health():
    new, old = "2026-11-04T12:00:00+00:00", "2026-11-03T12:00:00+00:00"
    idx = _notes_index([_note("b", 2, new, "critical"), _note("a", 1, new),
                        _note("c", 3, new), _note("old", 0, old)])
    doc = home.notes(idx, now=datetime(2026, 11, 4, 13, tzinfo=timezone.utc))
    assert [r["id"] for r in doc["rows"]] == ["a", "b"] and doc["total"] == 3
    assert doc["stale"] is False
    page = _html(idx, resolve("/"))
    assert "Things to know" in page and "insight old" not in page
    assert '<a href="/page/incidents">insight a</a>' in page
    assert "<strong>You:</strong> nothing" in page and "1 more not shown" in page
    assert page.index("Things to know") < page.index("<h2>system health</h2>")
    assert render_json.payload(idx, resolve("/"))["notes"]["rows"][0]["id"] == "a"


def test_notes_with_mixed_sort_types_still_render():
    """I13239 (codex db2789e362c3f656): an int beside a str in the sort field
    raised TypeError and took the whole home page down."""
    new = "2026-11-04T12:00:00+00:00"
    idx = _notes_index([_note("s", "high", new), _note("n", 1, new), _note("z", None, new)])
    doc = home.notes(idx, now=datetime(2026, 11, 4, 13, tzinfo=timezone.utc))
    assert [r["id"] for r in doc["rows"]] == ["n", "s"] and doc["total"] == 3
    assert "Things to know" in _html(idx, resolve("/"))


def test_old_notes_say_they_are_old_and_no_notes_say_so():
    idx = _notes_index([_note("a", 1, "2026-11-01T12:00:00+00:00")])
    assert home.notes(idx, now=datetime(2026, 11, 4, tzinfo=timezone.utc))["stale"] is True
    assert "nothing published yet" in _html(_notes_index([]), resolve("/"))


def test_health_does_not_repeat_what_home_shows():
    """Brian 2026-11-04: home is the summary, /health only the detail."""
    from console.render.html import health_page, landing_page
    idx = _index()
    home, health = landing_page(idx), health_page(idx)
    for marker in ('<h2>exceptions</h2>', 'waiting on Brian', 'global-search'):
        assert marker in home and marker not in health
    assert "registries" in health and "registries" not in home.split("system health details")[0]
