"""alpha-engine-config-I11805: honest empty and paging lines on filtered lists.

A filter that matches nothing is not a source that reported nothing, and the
"showing" line names where every row of the kind went — its parts add up to
the denominator the JSON representation reports as `of` (§3.8).
"""
from __future__ import annotations

import re

from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import json as render_json
from console.server.app import _html
from console.server.router import resolve


def _index(entities) -> Index:
    index = Index()
    index.add_result(AdapterResult(
        name="f", status=AdapterStatus.OK, claim_class=ClaimClass.DECLARATION,
        entities=tuple(entities), edges=(),
    ))
    index.finalize()
    return index


def _component(i: int, state: State) -> Entity:
    return Entity(kind=Kind.COMPONENT, id=f"svc-{i:03d}", state=state,
                  provenance=Provenance(source="fixture", as_of="2026-10-26T10:00:00Z"))


def _run(rid: str, as_of: str) -> Entity:
    return Entity(kind=Kind.RUN, id=rid, state=State.HEALTHY,
                  provenance=Provenance(source="fixture", as_of=as_of))


FORTY = [_component(i, State.FAILED if i < 6 else State.HEALTHY) for i in range(40)]


def _page(index, route):
    path, _, query = route.partition("?")
    return _html(index, resolve(path, query))


def _shown(page: str) -> str:
    match = re.search(r'<p class="list-shown">([^<]*)</p>', page)
    assert match, "no showing line"
    return match.group(1)


def _adds_up(line: str) -> None:
    """Every part names a count; shown + the rest == the denominator."""
    head = re.match(r"showing (\d+) of (\d+) ", line)
    assert head, line
    shown, total = int(head.group(1)), int(head.group(2))
    rest = sum(int(n) for n in re.findall(r" · (\d+) ", line))
    assert shown + rest == total, line


def test_a_filter_that_matches_nothing_says_so_and_names_the_filter():
    page = _page(_index(FORTY), "/component?state=BOGUS")
    assert "the source reported none" not in page
    assert "No components match the filter state=BOGUS" in page
    assert "none of the 40 components" in page


def test_an_empty_source_still_says_the_source_reported_none():
    page = _page(_index([_component(0, State.HEALTHY)]), "/run")
    assert "No entities — the source reported none." in page


def test_the_showing_line_says_what_the_filter_hid_out_of_what():
    page = _page(_index(FORTY), "/component?state=FAILED")
    line = _shown(page)
    assert line == ("showing 6 of 40 components · 34 hidden by filter "
                    "state=FAILED · page 1 of 1")
    assert "filtered · page" not in page
    _adds_up(line)


def test_an_unfiltered_list_names_no_filter_and_says_page_n_of_m():
    line = _shown(_page(_index(FORTY), "/component"))
    assert line == "showing 40 of 40 components · page 1 of 1"


def test_paging_counts_the_rows_on_other_pages():
    many = [_component(i, State.HEALTHY) for i in range(120)]
    for page_no, shown in ((1, 50), (3, 20)):
        line = _shown(_page(_index(many), f"/component?page={page_no}"))
        assert line.startswith(f"showing {shown} of 120 components")
        assert f"page {page_no} of 3" in line
        _adds_up(line)


def test_a_page_past_the_end_says_so_rather_than_blaming_the_source():
    page = _page(_index(FORTY), "/component?page=4")
    assert "the source reported none" not in page
    assert "No rows on page 4" in page


def test_folded_runs_are_counted_so_the_collapsed_line_adds_up():
    runs = [_run("job-a@2026-10-01T01:00:00Z", "2026-10-01T01:00:00Z"),
            _run("job-a@2026-10-01T02:00:00Z", "2026-10-01T02:00:00Z"),
            _run("job-b@2026-10-01T01:30:00Z", "2026-10-01T01:30:00Z")]
    line = _shown(_page(_index(runs), "/run"))
    assert "1 older runs folded into their job" in line
    _adds_up(line)


def test_the_html_denominator_is_the_json_of():
    index = _index(FORTY)
    req = resolve("/component", "state=FAILED")
    doc = render_json.payload(index, req)
    assert f"showing {doc['showing']} of {doc['of']} " in _shown(_html(index, req))
