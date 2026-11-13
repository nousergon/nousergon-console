"""A `decisions` section: a page that opts in to acting (Brian 2026-11-13:
pages are read-only by default, and a specific page may be interactive).

The guarantees under test: the write path is declared, same-origin and never
a console route, so the console itself receives no write; the cards carry the
row's own values and the declared buttons; a demo shows no buttons; and the
browser posts the declared body (`console.js`)."""

from __future__ import annotations

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
from console.server.router import resolve

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def _break(pid: str, headline: str | None, age: int, tenant: str = "acme") -> Entity:
    fields = {
        "proposal_id": {"value": pid, "render": "text"},
        "tenant": {"value": tenant, "render": "text"},
        "our_books": {"value": "$1,000.00", "render": "text"},
        "broker_says": {"value": "$1,107.69", "render": "text"},
        "difference": {"value": "+$107.69", "render": "text"},
        "proposed_fix": {"value": "Find the missing cash entry.", "render": "text"},
        "age_trading_days": {"value": age, "render": "value"},
    }
    if headline is not None:
        fields["headline"] = {"value": headline, "render": "text"}
    return Entity(kind=Kind.SIGNAL, id=f"queue:{pid}", state=State.HEALTHY,
                  provenance=Provenance("s3://b/scores.json", "2026-11-12T21:45:00+00:00",
                                        "https://e/scores"),
                  facets={"back_office": "queue", "tenant": tenant},
                  detail={"fields": fields})


ROWS = (
    _break("p1", "IB shows $107.69 more cash than our books expect", 6),
    _break("p2", "<script>alert(1)</script>", 3),
)

ACT = {
    "post": "/back-office/decide",
    "key": "proposal_id",
    "send": ["tenant"],
    "choice_field": "verdict",
    "headline": "headline",
    "choices": [{"value": "agree", "label": "Approve the fix", "primary": True},
                {"value": "defer", "label": "Not now"}],
    "change": {"value": "correct", "label": "Change the fix",
               "fields": {"action": [{"value": "investigate_cash",
                                      "label": "Find the missing cash entry"}]}},
}


def _decl(**act):
    return {"back-office": {
        "title": "Back office", "question": "What waits for a decision?",
        "sections": [{"type": "decisions", "title": "Decide these",
                      "select": {"kind": "signal", "facets": {"back_office": "queue"}},
                      "fields": [{"field": "our_books", "label": "Our books"},
                                 {"field": "broker_says", "label": "IB says"},
                                 "difference", {"field": "proposed_fix", "label": "Proposed fix"}],
                      "sort": "-age_trading_days",
                      "act": {**ACT, **act}}]}}


def _index(decl=None, rows=ROWS):
    idx = Index()
    idx.add_result(AdapterResult(name="fixture", status=AdapterStatus.OK,
                                 claim_class=ClaimClass.OBSERVATION, entities=rows))
    pages.attach(idx, pages.parse(decl or _decl()))
    idx.finalize()
    return idx


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    evaluate = pages.evaluate

    def at_now(index, p, params=None, now=None):
        return evaluate(index, p, params, now=now or NOW)

    monkeypatch.setattr(pages, "evaluate", at_now)


# ------------------------------------------------------------- declaration --

@pytest.mark.parametrize("post", [
    "https://evil.example/x", "//evil.example/x", "back-office/decide", "/a/../b",
    "/page/back-office", "/actions", "/console.js", "/vendor/uplot/LICENSE", "/", "",
])
def test_the_write_path_is_same_origin_and_never_a_console_route(post):
    with pytest.raises(pages.PagesConfigError, match="act.post"):
        pages.parse(_decl(post=post))


@pytest.mark.parametrize("change, message", [
    ({"choices": []}, "at least one button"),
    ({"key": "Proposal Id"}, "field name"),
    ({"choices": [{"value": "a", "label": "A"}, {"value": "a", "label": "B"}]}, "same value"),
    ({"send": ["verdict"]}, "posted twice"),
    ({"bogus": 1}, "unknown key"),
    ({"choices": ["agree"]}, "must be a mapping"),
    ({"change": {"value": "c", "label": "C"}}, "`fields` must map"),
])
def test_a_bad_act_fails_the_build_by_name(change, message):
    with pytest.raises(pages.PagesConfigError, match=message):
        pages.parse(_decl(**change))


def test_a_section_without_act_and_act_on_a_table_are_refused():
    decl = _decl()
    del decl["back-office"]["sections"][0]["act"]
    with pytest.raises(pages.PagesConfigError, match="must declare where"):
        pages.parse(decl)
    table = {"p": {"title": "t", "question": "q", "sections": [
        {"type": "table", "title": "x", "select": {"facets": {"a": "b"}}, "act": ACT}]}}
    with pytest.raises(pages.PagesConfigError, match="only a decisions section"):
        pages.parse(table)


def test_a_table_key_is_refused_on_a_decisions_section():
    decl = _decl()
    decl["back-office"]["sections"][0]["sortable"] = ["difference"]
    with pytest.raises(pages.PagesConfigError, match="does not apply"):
        pages.parse(decl)


def test_a_bad_limit_is_refused():
    decl = _decl()
    decl["back-office"]["sections"][0]["limit"] = 0
    with pytest.raises(pages.PagesConfigError, match="`limit`"):
        pages.parse(decl)


def test_console_routes_are_recognised():
    assert pages.is_console_route("/page/x")
    assert pages.is_console_route("/styles.css")
    assert not pages.is_console_route("/back-office/decide")


# -------------------------------------------------------------- evaluation --

def _section(idx):
    page = render_json.payload(idx, resolve("/page/back-office", ""))["page"]
    return page["sections"][0]


def test_cards_carry_the_rows_values_and_the_declared_write():
    sec = _section(_index())
    assert [r["fields"]["proposal_id"] for r in sec["rows"]] == ["p1", "p2"]  # oldest first
    assert sec["act"]["post"] == "/back-office/decide"
    assert sec["act"]["choice_field"] == "verdict"
    assert sec["labels"][1] == ["broker_says", "IB says"]
    assert sec["rows"][0]["fields"]["tenant"] == "acme"


def test_html_cards_show_plain_words_and_post_the_declared_body():
    html = render_html.page_page(_index(), "back-office", {})
    assert "IB shows $107.69 more cash than our books expect" in html
    assert "<dt>IB says</dt><dd>$1,107.69</dd>" in html
    assert 'data-post="/back-office/decide"' in html
    assert 'data-choice-field="verdict"' in html
    assert '<input type="hidden" name="proposal_id" value="p1">' in html
    assert '<input type="hidden" name="tenant" value="acme">' in html
    assert 'data-choice="agree"' in html and "Approve the fix" in html
    assert 'data-choice="correct" data-change="1"' in html
    assert '<option value="investigate_cash">Find the missing cash entry</option>' in html
    assert 'textarea name="note"' in html
    assert "<script>alert(1)</script>" not in html  # row text is escaped
    # the console itself serves no write at the declared path
    with pytest.raises(Exception):
        resolve("/back-office/decide", "")


def test_a_demo_shows_the_cards_without_buttons():
    html = render_html._page_decisions(_section(_index()), demo=True)
    assert "IB shows" in html
    assert "data-choice" not in html and "page-act" not in html


def test_a_row_without_its_key_cannot_be_decided():
    row = _break("", "no id here", 1)
    sec = _section(_index(rows=(row,)))
    html = render_html._page_decisions(sec)
    assert "cannot be decided here" in html and "data-choice" not in html


def test_a_card_falls_back_to_the_row_label_and_no_note_when_declared_off():
    sec = _section(_index(decl=_decl(note=False, headline=None, change=None)))
    html = render_html._page_decisions(sec)
    assert "textarea" not in html and "decision-change" not in html
    assert 'class="decision-title">queue:p1<' in html


def test_nothing_waiting_says_so():
    sec = _section(_index(rows=()))
    assert "no row matches" in render_html._page_decisions(sec)


def test_more_rows_than_the_limit_say_how_many():
    decl = _decl()
    decl["back-office"]["sections"][0]["limit"] = 1
    sec = _section(_index(decl=decl))
    assert "showing 1 of 2" in render_html._page_decisions(sec)


def test_the_browser_half_posts_json_to_the_declared_path_only():
    js = open("console/static/console.js", encoding="utf-8").read()
    block = js[js.index("function sendPageAct"):js.index("document.addEventListener(\"click\"", js.index("function sendPageAct"))]
    assert '"Content-Type": "application/json"' in block
    assert "credentials: \"same-origin\"" in block
    assert re.search(r'indexOf\("//"\) !== -1', block)


def test_only_a_2xx_json_answer_without_an_error_is_a_save():
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    assert node, "node is required to test console.js (preinstalled on CI runners)"
    cases = [
        [201, "application/json", '{"decision_id": "d1", "decided_by": "brian"}'],
        [403, "application/json", '{"error": "this identity is not an approver"}'],
        [200, "text/html", "<html>sign in</html>"],
        [200, "application/json", '{"ok": false}'],
        [502, "text/plain", "bad gateway"],
    ]
    prog = ("const m=require(process.argv[1]);const c=JSON.parse(process.argv[2]);"
            "console.log(JSON.stringify(c.map(x=>m.pageActReply(x[0],x[1],x[2]))))")
    out = json.loads(subprocess.run([node, "-e", prog, "./console/static/console.js",
                                     json.dumps(cases)],
                                    capture_output=True, text=True, check=True).stdout)
    assert out[0]["ok"] is True and out[0]["decided_by"] == "brian"
    assert out[1] == {"ok": False, "error": "this identity is not an approver (HTTP 403)"}
    assert [o["ok"] for o in out[2:]] == [False, False, False]
