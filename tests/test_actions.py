"""Your actions (`console/index/actions.py`, `/actions`).

The page lists open issues carrying a declared label with a tick box each; a
tick writes the done label and a comment to the issue, and nothing about it
is kept on the console. Hermetic: the host is a recorded fake.
"""
from __future__ import annotations

import http.client
import json
import threading

import pytest

from console.index import actions
from console.index.graph import Index
from console.model.envelope import AdapterStatus
from console.render import json as render_json
from console.server.app import _html, serve
from console.server.router import resolve

DECL = {
    "org": "acme", "repo": "tracker", "labels": ["gate:operator"],
    "done_label": "operator-done",
    "token_command": ["/bin/echo", "read-token"],
    "write_token_command": ["/bin/echo", "write-token"],
}


def _issue(n, title="Rotate the key", labels=("gate:operator",), body="Run the script.\n\nMore.",
           created="2026-11-01T10:00:00Z", pr=False):
    out = {"number": n, "title": title, "html_url": f"https://example.test/{n}",
           "labels": [{"name": lab} for lab in labels], "body": body,
           "created_at": created, "updated_at": created}
    if pr:
        out["pull_request"] = {}
    return out


class FakeHost:
    def __init__(self, issues):
        self.issues = issues
        self.calls: list[tuple[str, str, str, object]] = []

    def __call__(self, method, url, token, body):
        self.calls.append((method, url, token, body))
        if method == "GET":
            return self.issues
        return {}


def _index(decl=None, issues=None):
    d = actions.parse(decl or DECL)
    host = FakeHost(issues if issues is not None else [
        _issue(1), _issue(2, "Approve the grant", labels=("gate:operator", "operator-done"),
                          created="2026-10-30T10:00:00Z"),
        _issue(3, pr=True)])
    found, result = actions.read(d, "2026-11-04T12:00:00Z", request=host)
    idx = Index()
    idx.add_result(result)
    actions.attach(idx, found)
    idx.finalize()
    return idx, host


# ------------------------------------------------------------------ parse ---

def test_no_block_is_no_page():
    assert actions.parse(None) is None


@pytest.mark.parametrize("bad", [
    {**DECL, "labels": []},
    {**DECL, "done_label": "gate:operator"},
    {**DECL, "repo": "a/b"},
    {**DECL, "token_command": None},
    {**DECL, "surprise": 1},
])
def test_a_block_it_cannot_honour_is_refused(bad):
    with pytest.raises(actions.ActionsConfigError):
        actions.parse(bad)


def test_a_bad_block_is_a_failed_source_not_a_crash():
    from console.config import build_index

    idx = build_index({"actions": {**DECL, "labels": []}})
    failed = [a for a in idx.build_info.adapters if a.name == actions.SOURCE_NAME]
    assert len(failed) == 1 and failed[0].status == "failed"
    assert "no actions are declared" in _html(idx, resolve("/actions"))


# ------------------------------------------------------------------- read ---

def test_lists_open_labelled_issues_not_done_first_and_skips_prs():
    idx, host = _index()
    found = actions.listing(idx)
    assert [a.number for a in found.actions] == [1, 2]
    assert [a.done for a in found.actions] == [False, True]
    assert found.actions[0].summary == "Run the script."
    method, url, token, _ = host.calls[0]
    assert method == "GET" and token == "read-token"
    assert "/repos/acme/tracker/issues?" in url and "labels=gate%3Aoperator" in url
    assert "state=open" in url


def test_a_failed_read_says_why_and_never_reads_as_nothing_to_do():
    d = actions.parse(DECL)

    def broken(*_):
        raise RuntimeError("HTTP 401: Unauthorized")

    found, result = actions.read(d, "2026-11-04T12:00:00Z", request=broken)
    assert result.status is AdapterStatus.FAILED
    assert "401" in result.unavailable[0]
    idx = Index()
    actions.attach(idx, found)
    idx.finalize()
    html = _html(idx, resolve("/actions"))
    assert "could not read the actions" in html and "Nothing to do" not in html


def test_summary_is_the_first_prose_paragraph():
    assert actions.summary("## Heading\n\n**Do** this `now`.\n\nLater.") == "Do this now."
    assert actions.summary(None) == ""
    assert actions.summary("x " * 400).endswith("…")


# ------------------------------------------------------------------ write ---

def test_tick_adds_the_done_label_and_says_who_on_the_issue():
    idx, _ = _index()
    host = FakeHost([])
    act = actions.tick(idx, 1, True, "brian@example.test", request=host)
    assert act.done
    assert host.calls[0][:2] == ("POST", "https://api.github.com/repos/acme/tracker/issues/1/labels")
    assert host.calls[0][2] == "write-token"
    assert host.calls[0][3] == {"labels": ["operator-done"]}
    assert host.calls[1][1].endswith("/issues/1/comments")
    assert "brian@example.test" in host.calls[1][3]["body"]
    # The page answering the POST shows it before the next rebuild.
    assert actions.listing(idx).actions[0].done


def test_untick_removes_only_the_done_label():
    idx, _ = _index()
    host = FakeHost([])
    act = actions.tick(idx, 2, False, None, request=host)
    assert not act.done
    assert host.calls[0][0] == "DELETE"
    assert host.calls[0][1].endswith("/issues/2/labels/operator-done")


def test_an_issue_not_on_the_list_cannot_be_ticked():
    idx, _ = _index()
    host = FakeHost([])
    with pytest.raises(actions.TickRefused) as exc:
        actions.tick(idx, 999, True, None, request=host)
    assert exc.value.status == 404 and host.calls == []


def test_no_write_command_means_no_write():
    idx, _ = _index({**DECL, "write_token_command": None})
    with pytest.raises(actions.TickRefused) as exc:
        actions.tick(idx, 1, True, None, request=FakeHost([]))
    assert exc.value.status == 403
    assert "ticking is off" in _html(idx, resolve("/actions"))


def test_a_refused_write_changes_nothing_here():
    idx, _ = _index()

    def refuses(*_):
        raise RuntimeError("HTTP 403: Forbidden")

    with pytest.raises(actions.TickRefused) as exc:
        actions.tick(idx, 1, True, None, request=refuses)
    assert exc.value.status == 502
    assert not actions.listing(idx).actions[0].done


# ----------------------------------------------------------------- render ---

def test_both_representations():
    idx, _ = _index()
    doc = render_json.payload(idx, resolve("/actions"))
    assert doc["view"] == "actions" and doc["open"] == 1 and doc["done"] == 1
    assert doc["actions"][0]["ref"] == "tracker-I1"
    html = _html(idx, resolve("/actions"))
    assert 'action="/actions/1"' in html and 'name="done" value="1"' in html
    assert 'action="/actions/2"' in html and 'name="done" value="0"' in html
    assert "Done, waiting for a check" in html


def test_the_menu_links_your_actions():
    idx = Index()
    idx.finalize()
    html = _html(idx, resolve("/"))
    nav = html[html.index('<nav class="site-nav">'):]
    assert '<a href="/actions">your actions</a>' in nav[:nav.index("</nav>")]


# ------------------------------------------------------------------- HTTP ---

@pytest.fixture()
def live(monkeypatch):
    idx, _ = _index()
    host = FakeHost([])
    real_tick = actions.tick
    monkeypatch.setattr(actions, "tick",
                        lambda index, n, done, actor: real_tick(index, n, done, actor, request=host))
    server = serve(idx, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], host, idx
    server.shutdown()
    server.server_close()


def _post(port, path, body="done=1", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    h = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
    conn.request("POST", path, body=body, headers=h)
    resp = conn.getresponse()
    out = resp.status, dict(resp.getheaders()), resp.read().decode()
    conn.close()
    return out


def test_a_same_origin_tick_writes_and_redirects(live):
    port, host, idx = live
    status, headers, _ = _post(port, "/actions/1", headers={
        "Origin": f"http://127.0.0.1:{port}",
        "Cf-Access-Authenticated-User-Email": "brian@example.test"})
    assert status == 303 and headers["Location"] == "/actions#a1"
    assert host.calls[0][3] == {"labels": ["operator-done"]}
    assert "brian@example.test" in host.calls[1][3]["body"]


def test_a_cross_site_post_is_refused(live):
    port, host, _ = live
    status, _, _ = _post(port, "/actions/1", headers={"Origin": "https://evil.test"})
    assert status == 403 and host.calls == []
    status, _, _ = _post(port, "/actions/1")  # no Origin, no Referer
    assert status == 403 and host.calls == []


def test_every_other_post_is_405(live):
    port, host, _ = live
    status, _, _ = _post(port, "/exceptions", headers={"Origin": f"http://127.0.0.1:{port}"})
    assert status == 405 and host.calls == []


def test_an_unlisted_number_is_404_and_says_so(live):
    port, host, _ = live
    status, _, body = _post(port, "/actions/77", headers={"Origin": f"http://127.0.0.1:{port}"})
    assert status == 404 and "not on the actions list" in body and host.calls == []


# ----------------------------------------------------------------- timing ---

@pytest.mark.parametrize("body,expected", [
    ("Do it.\n\nDue: 2026-11-06\nWhen: after the 16:30 ET close", ("2026-11-06", "due", "after the 16:30 ET close")),
    ("**Due:** 2026-11-07", ("2026-11-07", "due", None)),
    ("Default: option A after 2026-11-10", ("2026-11-10", "default applies", None)),
    ("Re-exam: 2026-12-01\n- **Window:** weekend only", ("2026-12-01", "re-exam", "weekend only")),
    ("nothing declared", (None, None, None)),
])
def test_due_and_when_come_from_the_issue_body(body, expected):
    assert actions.timing(body) == expected


def test_a_milestone_date_is_the_fallback():
    assert actions.timing("x", {"due_on": "2026-11-20T08:00:00Z"}) == ("2026-11-20", "milestone", None)


def test_soonest_due_first_then_undated_and_the_table_shows_both_columns():
    idx, _ = _index(issues=[
        _issue(5, "Undated", created="2026-10-01T00:00:00Z"),
        _issue(6, "Later", body="x\n\nDue: 2099-01-09"),
        _issue(7, "Sooner", body="x\n\nDue: 2099-01-02\nWhen: outside 09:00-16:30 ET"),
    ])
    assert [a.number for a in actions.listing(idx).actions] == [7, 6, 5]
    html = _html(idx, resolve("/actions"))
    assert "<th>due</th>" in html and "<th>when it can happen</th>" in html
    assert "outside 09:00-16:30 ET" in html and "2099-01-02" in html
    assert "any time" in html and "no date" in html
    # The tick is a cell in the same row as the ask, never a line above it.
    row = html[html.index('<tr id="a7"'):]
    row = row[:row.index("</tr>")]
    assert 'action="/actions/7"' in row and "Sooner" in row
    doc = render_json.payload(idx, resolve("/actions"))
    assert doc["actions"][0]["due"] == "2099-01-02" and doc["actions"][0]["when"].startswith("outside")
