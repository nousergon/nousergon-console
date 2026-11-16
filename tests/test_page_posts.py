"""A `posts` section: rows read as a feed of cards, each with its full text
(Brian 2026-11-16: "each thread's full updates in a facebook post style update
log, one post for each thread ... I don't want to have to open the thread").

Under test: the declaration is checked by name; each card carries its headline,
its labelled facts and its markdown body rendered through the document path
(raw HTML never passes); and the JSON payload carries the same cards."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from console.index import pages
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import html as render_html
from console.render import json as render_json
from console.server.router import resolve

NOW = datetime(2026, 11, 16, 18, 0, tzinfo=timezone.utc)


def _thread(tid: str, title: str, log: str, at: str, ask: str = "") -> Entity:
    fields = {
        "title": {"value": title, "render": "text"},
        "status": {"value": "working", "render": "text"},
        "ask": {"value": ask, "render": "text"},
        "log": {"value": log, "render": "text"},
        "at": {"value": at, "render": "text"},
    }
    return Entity(kind=Kind.RUN, id=f"thread:{tid}", state=State.HEALTHY,
                  provenance=Provenance("s3://b/index.json", at, "https://e/index"),
                  facets={"record": "thread-post"}, detail={"fields": fields})


ROWS = (
    _thread("a", "Staging", "### 11-16 09:00\n**Trader session ok.**\n\nAll **green**.",
            "2026-11-16T17:00:00+00:00", ask="Run 1116a"),
    _thread("b", "Console", "<script>alert(1)</script> plain text",
            "2026-11-16T12:00:00+00:00"),
)


def _decl(**extra: object) -> dict[str, object]:
    section: dict[str, object] = {
        "type": "posts", "title": "Threads",
        "select": {"kind": "run", "facets": {"record": "thread-post"}},
        "headline": "title", "body": "log",
        "fields": ["status", {"field": "ask", "label": "Waiting on you"}],
        "sort": "-at", **extra}
    return {"thread-updates": {"title": "Thread updates", "question": "q",
                               "sections": [section]}}


def _index(decl: dict[str, object] | None = None) -> Index:
    idx = Index()
    idx.add_result(AdapterResult(name="fixture", status=AdapterStatus.OK,
                                 claim_class=ClaimClass.OBSERVATION, entities=ROWS))
    pages.attach(idx, pages.parse(decl or _decl()))
    idx.finalize()
    return idx


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluate = pages.evaluate

    def at_now(index, p, params=None, now=None):  # type: ignore[no-untyped-def]
        return evaluate(index, p, params, now=now or NOW)

    monkeypatch.setattr(pages, "evaluate", at_now)


@pytest.mark.parametrize("extra, message", [
    ({"body": ""}, "body"),
    ({"act": {"post": "/x"}}, "does not apply"),
    ({"sortable": ["at"]}, "does not apply"),
    ({"limit": 0}, "limit"),
])
def test_a_bad_posts_declaration_fails_by_name(extra: dict[str, object], message: str) -> None:
    with pytest.raises(pages.PagesConfigError, match=message):
        pages.parse(_decl(**extra))


def test_each_row_is_one_card_with_its_full_text_newest_first() -> None:
    html = render_html.page_page(_index(), "thread-updates", {})
    assert html.count('<article class="post page-post">') == 2
    assert html.index("Staging") < html.index("Console")
    assert "<strong>Trader session ok.</strong>" in html
    assert "Waiting on you" in html and "Run 1116a" in html


def test_a_body_never_passes_raw_html() -> None:
    html = render_html.page_page(_index(), "thread-updates", {})
    assert "<script>alert(1)</script>" not in html


def test_an_empty_ask_is_not_shown_as_a_fact() -> None:
    html = render_html.page_page(_index(), "thread-updates", {})
    console_card = html[html.index(">Console<"):]
    assert "Waiting on you" not in console_card


def test_the_json_payload_carries_the_cards() -> None:
    page = render_json.payload(_index(), resolve("/page/thread-updates", ""))["page"]
    sec = page["sections"][0]
    assert sec["type"] == "posts" and sec["body"] == "log" and sec["headline"] == "title"
    assert [r["fields"]["title"] for r in sec["rows"]] == ["Staging", "Console"]
