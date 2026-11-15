"""A ruled decision says what was picked (Brian 2026-11-15: a brief he had
approved on the console was reported to him as still waiting, and the Done
row only said "done"). The pick is read back from the ruling comment the
console itself writes."""
from __future__ import annotations

from console.index import actions
from console.server.app import _html
from console.server.router import resolve

from tests.test_actions import DECL, FakeHost, _index, _issue

BODY = "**Brief:** Pick one.\n\n**Options:**\nA) Approve the brief\nB) Change it\n"


def test_ruling_parses_the_consoles_own_comment():
    assert actions.ruling("Ruling on the console by b@x: option A) Approve the brief\n\nNext: go") == (
        "A", "Approve the brief")
    assert actions.ruling("Marked done on the console by b@x.") is None
    assert actions.ruling(None) is None


def test_a_ruled_open_decision_shows_the_pick_from_its_comment():
    d = actions.parse(DECL)
    host = FakeHost([_issue(7, "Approve the brief", labels=("gate:operator", "operator-done"),
                            body=BODY)])
    host.prs["/issues/7/comments?per_page=100"] = [
        {"body": "Ruling on the console by brian@x: option A) Approve the brief\n\nNext: carry it out."}]
    found, _ = actions.read(d, "2026-11-15T12:00:00Z", request=host)
    assert found.actions[0].ruling == ("A", "Approve the brief")


def test_ticking_an_option_shows_the_pick_at_once():
    idx, _ = _index(issues=[_issue(7, "Approve the brief", body=BODY)])
    act = actions.tick(idx, 7, True, "brian@x", request=FakeHost([]), choice="A")
    assert act.ruling == ("A", "Approve the brief")
    html = _html(idx, resolve("/actions"))
    assert "You picked A) Approve the brief" in html
