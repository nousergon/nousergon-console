"""The second box on `/actions` (Brian 2026-11-14): "this needs an agent to
re-review it". ops#2324 had red CI, so it was not his to merge yet, and the
page gave him no way to say so. The box writes the declared ``agent_label``;
the row leaves his to-do list for "With agents" until an agent fixes it and
removes the label."""
from __future__ import annotations

import http.client
import json
import threading
import time

import pytest

from console.index import actions
from console.server.app import _html, serve
from console.server.router import resolve

from tests.test_actions import DECL, FakeHost, _index, _issue

AGENT = {**DECL, "agent_label": "needs-agent"}


def _agent_index(issues=None):
    return _index(AGENT, issues if issues is not None else [
        _issue(1, "Merge ops#2324", body="Merge https://github.com/acme/ops/pull/2324 now."),
        _issue(2, "Rule on it", body="**Options:**\nA) yes\nB) no\n"),
        _issue(4, "Already sent back", labels=("gate:operator", "needs-agent"),
               body="Merge https://github.com/acme/ops/pull/9."),
    ])


@pytest.mark.parametrize("bad", [
    {**AGENT, "agent_label": "operator-done"},
    {**AGENT, "agent_label": "gate:operator"},
    {**AGENT, "agent_label": ""},
])
def test_the_agent_label_must_be_its_own_label(bad):
    with pytest.raises(actions.ActionsConfigError, match="agent_label"):
        actions.parse(bad)


def test_a_flagged_row_is_with_agents_not_to_do_and_not_done():
    idx, _ = _agent_index()
    found = actions.listing(idx)
    flagged = next(a for a in found.actions if a.number == 4)
    assert flagged.agent and not flagged.actionable
    assert 4 in [a.number for a in found.not_ready]
    assert 4 not in [a.number for a in found.todo]
    assert 4 not in [a.number for a in found.done]


def test_ticking_the_box_writes_the_agent_label_and_says_what_happens_next():
    idx, _ = _agent_index()
    host = FakeHost([])
    act = actions.tick(idx, 1, True, "brian@example.test", request=host, flag="agent")
    assert act.agent and not act.done and not act.actionable
    assert host.calls[0][:2] == ("POST", "https://api.github.com/repos/acme/tracker/issues/1/labels")
    assert host.calls[0][3] == {"labels": ["needs-agent"]}
    body = host.calls[1][3]["body"]
    assert "brian@example.test" in body and "needs-agent" in body and "re-review" in body
    found = actions.listing(idx)
    assert 1 in [a.number for a in found.not_ready]


def test_unticking_takes_it_back_and_removes_only_the_agent_label():
    idx, _ = _agent_index()
    host = FakeHost([])
    act = actions.tick(idx, 4, False, None, request=host, flag="agent")
    assert not act.agent and act.actionable
    assert host.calls[0][0] == "DELETE" and host.calls[0][1].endswith("/labels/needs-agent")


def test_no_agent_label_declared_refuses_the_box_and_draws_none():
    idx, _ = _index()
    with pytest.raises(actions.TickRefused, match="agent_label"):
        actions.tick(idx, 1, True, None, request=FakeHost([]), flag="agent")
    assert 'name="flag"' not in _html(idx, resolve("/actions"))


def test_unknown_box_and_choice_with_agent_box_are_refused():
    idx, _ = _agent_index()
    with pytest.raises(actions.TickRefused, match="unknown box"):
        actions.tick(idx, 1, True, None, request=FakeHost([]), flag="other")
    with pytest.raises(actions.TickRefused, match="done box only"):
        actions.tick(idx, 2, True, None, request=FakeHost([]), flag="agent", choice="A")


def test_every_open_row_and_decision_card_offers_the_box():
    idx, _ = _agent_index()
    html = _html(idx, resolve("/actions"))
    assert html.count('name="flag" value="agent"') == 3
    assert "needs an agent" in html
    assert "sent back to an agent to re-review" in html
    assert '<details class="actions-waiting"><summary><h2>With agents · 1</h2>' in html


def test_a_flag_survives_a_rebuild_that_read_the_tracker_before_it():
    idx, _ = _agent_index()
    before = time.monotonic()
    actions.tick(idx, 1, True, None, request=FakeHost([]), flag="agent")
    # A rebuild whose read began before the tick still shows the flag.
    fresh, _ = _agent_index()
    actions.listing(fresh).read_started = before
    assert next(a for a in actions.listing(fresh).actions if a.number == 1).agent
    # and the done box is untouched by it.
    assert not next(a for a in actions.listing(fresh).actions if a.number == 1).done


@pytest.fixture()
def live(monkeypatch):
    idx, _ = _agent_index()
    host = FakeHost([])
    real_tick = actions.tick
    monkeypatch.setattr(actions, "tick",
                        lambda index, n, done, actor, **kw: real_tick(index, n, done, actor,
                                                                      request=host, **kw))
    server = serve(idx, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], host
    server.shutdown()
    server.server_close()


def test_the_script_post_says_which_part_of_the_page_the_row_now_belongs_in(live):
    port, host = live
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request("POST", "/actions/1", body="done=1&flag=agent", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
        "Origin": f"http://127.0.0.1:{port}"})
    resp = conn.getresponse()
    reply = json.loads(resp.read())
    conn.close()
    assert resp.status == 200 and reply["agent"] and reply["section"] == "agents"
    assert host.calls[0][3] == {"labels": ["needs-agent"]}
    assert 'name="flag" value="agent"' in reply["row"]
    actions.wait_for_comments()
