"""alpha-engine-config-I11805: states read worst first, everywhere.

A list's count by state used to be alphabetical — `1 DEGRADED · 1 FAILED ·
2 HEALTHY · 1 UNREPORTED` — so the worst state could sit in the middle of the
line, and the not-healthy rows under it kept source order, so an UNREPORTED
row could lead a FAILED one. Both now follow one order (`state_rank`): the
§5.1 exception states by severity, then other not-healthy values, then the
rest, HEALTHY last. HTML and JSON carry the same order (§3.8).
"""
from __future__ import annotations

import json

from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import json as render_json
from console.render.html import ordered_summary, state_rank
from console.server.app import _html
from console.server.router import resolve


def _comp(cid: str, state) -> Entity:
    return Entity(kind=Kind.COMPONENT, id=cid, state=state,
                  provenance=Provenance(source="fixture", as_of="2026-10-01T00:00:00Z"))


def _index(entities) -> Index:
    index = Index()
    index.add_result(AdapterResult(
        name="f", status=AdapterStatus.OK, claim_class=ClaimClass.DECLARATION,
        entities=tuple(entities),
    ))
    index.finalize()
    return index


# Source order deliberately puts the mildest exception first.
COMPONENTS = [
    _comp("comp-quiet", State.UNREPORTED),
    _comp("comp-ok-1", State.HEALTHY),
    _comp("comp-broken", State.FAILED),
    _comp("comp-ok-2", State.HEALTHY),
    _comp("comp-short", State.DEGRADED),
    _comp("comp-off", State.DISABLED),
]
WORST_FIRST = ["FAILED", "DEGRADED", "UNREPORTED", "DISABLED", "HEALTHY"]


def _page(index, route):
    path, _, query = route.partition("?")
    return _html(index, resolve(path, query))


def _positions(text: str, needles: list[str]) -> list[int]:
    return [text.index(n) for n in needles]


def test_state_rank_puts_exceptions_first_by_severity_and_healthy_last():
    values = ["HEALTHY", "open", "stale", "UNREPORTED", "RUNNING", "FAILED"]
    assert sorted(values, key=state_rank) == [
        "FAILED", "UNREPORTED", "stale", "RUNNING", "open", "HEALTHY"]


def test_a_list_summary_counts_worst_state_first():
    listing = render_json.list_rows(_index(COMPONENTS), resolve("/component"))
    assert list(listing.summary) == WORST_FIRST
    assert listing.summary["HEALTHY"] == 2


def test_not_healthy_rows_sort_by_severity_and_healthy_rows_keep_source_order():
    listing = render_json.list_rows(_index(COMPONENTS), resolve("/component"))
    assert [e.id for e in listing.rows] == [
        "comp-broken", "comp-short", "comp-quiet",   # worst first
        "comp-ok-1", "comp-ok-2", "comp-off",        # source order kept
    ]


def test_the_html_summary_line_reads_in_the_same_order_as_its_rows():
    page = _page(_index(COMPONENTS), "/component")
    line = page.split('<p class="list-summary">')[1].split("</p>")[0]
    counts = [f"{n} {s}</span>" for n, s in
              [(1, "FAILED"), (1, "DEGRADED"), (1, "UNREPORTED"),
               (1, "DISABLED"), (2, "HEALTHY")]]
    found = _positions(line, counts)
    assert found == sorted(found)
    table = page.split("<tbody>")[1]
    rows = _positions(table, [">comp-broken<", ">comp-short<", ">comp-quiet<"])
    assert rows == sorted(rows)


def test_list_json_summary_carries_the_same_order():
    doc = json.loads(json.dumps(
        render_json.payload(_index(COMPONENTS), resolve("/component")), default=str))
    assert list(doc["summary"]) == WORST_FIRST
    assert [e["id"] for e in doc["entities"]][:3] == [
        "comp-broken", "comp-short", "comp-quiet"]


def test_search_and_registry_counts_read_worst_first():
    index = _index(COMPONENTS)
    search = _page(index, "/search?q=comp")
    line = search.split('<p class="list-summary">')[1].split("</p>")[0]
    found = _positions(line, ["FAILED</span>", "DEGRADED</span>",
                              "UNREPORTED</span>", "HEALTHY</span>"])
    assert found == sorted(found)
    view = render_json.registry_view(index, "f")
    assert list(view.summary) == WORST_FIRST


def test_ordered_summary_keeps_every_count():
    summary = {"HEALTHY": 3, "ABSENT": 1, "closed": 2, "FAILED": 4}
    out = ordered_summary(summary)
    assert out == summary
    assert list(out) == ["FAILED", "ABSENT", "closed", "HEALTHY"]
