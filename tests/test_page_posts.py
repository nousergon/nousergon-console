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


# A post that names an open decision carries its option buttons, so the
# ruling is made on the post (Brian 2026-11-16: "make all decisions on the
# console page alone"). The buttons are /actions' own: same card, same bar,
# same POST to the issue.

OPTIONS = ("Pick one.\n\n**Options:** A) My pick: Ship — reversible "
           "B) Other: Wait — more evidence\n\n**Recommendation:** A — reversible.")


def _index_with_decisions(ask: str, fields: list[str] | None = None) -> Index:
    from console.index import actions
    from tests.test_actions import DECL, FakeHost, _issue

    host = FakeHost([_issue(7, "Approve the rules", body=OPTIONS), _issue(70, "Other", body=OPTIONS)])
    found, result = actions.read(actions.parse(DECL), "2026-11-16T12:00:00Z", request=host)
    rows = (_thread("a", "Back office", "update", "2026-11-16T17:00:00+00:00", ask=ask),
            _thread("b", "Console", "plain", "2026-11-16T12:00:00+00:00"))
    idx = Index()
    idx.add_result(result)
    idx.add_result(AdapterResult(name="fixture", status=AdapterStatus.OK,
                                 claim_class=ClaimClass.OBSERVATION, entities=rows))
    actions.attach(idx, found)
    pages.attach(idx, pages.parse(_decl(decisions=fields if fields is not None else ["ask"])))
    idx.finalize()
    return idx


@pytest.mark.parametrize("ask", [
    "Approve the rules: https://example.test/7",
    "Approve the rules (tracker-I7)",
])
def test_a_post_naming_an_open_decision_carries_its_buttons(ask: str) -> None:
    html = render_html.page_page(_index_with_decisions(ask), "thread-updates", {})
    card = html[html.index(">Back office<"):html.index(">Console<")]
    assert 'class="decision docket-decision"' in card
    assert 'action="/actions/7"' in card and 'data-docket-choice="A"' in card
    assert 'action="/actions/70"' not in html
    assert html.count('class="docket-bar"') == 1


def test_no_named_decision_means_no_buttons_and_no_bar() -> None:
    for ask, fields in (("https://example.test/77", None), ("https://example.test/7", [])):
        html = render_html.page_page(_index_with_decisions(ask, fields), "thread-updates", {})
        assert "docket-decision" not in html and "docket-bar" not in html


def test_decisions_must_name_fields() -> None:
    with pytest.raises(pages.PagesConfigError, match="decisions"):
        pages.parse(_decl(decisions="ask"))
