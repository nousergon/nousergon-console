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
    def __init__(self, issues, closed=(), prs=None):
        self.issues = issues
        self.closed = list(closed)
        self.prs = prs or {}
        self.calls: list[tuple[str, str, str, object]] = []

    def __call__(self, method, url, token, body):
        self.calls.append((method, url, token, body))
        if method == "GET":
            if "state=closed" in url:
                return self.closed
            for key, pr in self.prs.items():
                if url.endswith(key):
                    if isinstance(pr, Exception):
                        raise pr
                    return pr
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
    assert "<h2>Done · 1</h2>" in html


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
                        lambda index, n, done, actor, **kw: real_tick(index, n, done, actor,
                                                                      request=host, **kw))
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
    actions.wait_for_comments()
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


# ------------------------------------------- a tick without a page load ---
# Brian 2026-11-05: "check the box and then don't lock up the screen for it to
# shift". The page's script posts for JSON; the server answers as soon as the
# LABEL is on the issue, and the comment follows off the request thread.

_JSON = {"Accept": "application/json"}


def test_a_json_tick_answers_with_the_confirmed_row_not_a_redirect(live):
    port, host, idx = live
    status, headers, body = _post(port, "/actions/1", headers={
        **_JSON, "Origin": f"http://127.0.0.1:{port}"})
    assert status == 200 and headers["Content-Type"].startswith("application/json")
    reply = json.loads(body)
    assert reply["ok"] is True and reply["number"] == 1 and reply["done"] is True
    assert reply["actionable"] is False
    # The row as /actions now renders it: ticked, its button an untick.
    assert reply["row"].startswith('<tr id="a1" class="action action-done">')
    assert 'name="done" value="0"' in reply["row"]
    assert actions.listing(idx).actions[0].done
    actions.wait_for_comments()
    assert [c[1].rsplit("/", 1)[1] for c in host.calls] == ["labels", "comments"]


def test_the_reply_does_not_wait_for_the_comment(monkeypatch):
    """The label is the record and the reader waits for it alone: a comment
    still in flight never holds the response."""
    idx, _ = _index()
    release = threading.Event()
    calls = []

    def host(method, url, token, body):
        calls.append(url.rsplit("/", 1)[1])
        if url.endswith("/comments"):
            assert release.wait(10)
        return {}

    real_tick = actions.tick
    monkeypatch.setattr(actions, "tick", lambda index, n, done, actor, **kw:
                        real_tick(index, n, done, actor, request=host, **kw))
    server = serve(idx, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        status, _, body = _post(port, "/actions/1", headers={
            **_JSON, "Origin": f"http://127.0.0.1:{port}"})
        assert status == 200 and json.loads(body)["ok"] is True
        assert not release.is_set()  # answered while the comment was blocked
        release.set()
        actions.wait_for_comments()
        assert calls == ["labels", "comments"]
    finally:
        release.set()
        server.shutdown()
        server.server_close()


def test_a_refused_json_tick_says_why_in_json_and_changes_nothing(live):
    port, host, idx = live
    status, _, body = _post(port, "/actions/77", headers={
        **_JSON, "Origin": f"http://127.0.0.1:{port}"})
    assert status == 404
    assert json.loads(body) == {"ok": False, "error": "Not saved: #77 is not on the actions list"}
    status, _, body = _post(port, "/actions/1", headers={**_JSON, "Origin": "https://evil.test"})
    assert status == 403 and json.loads(body)["ok"] is False
    assert host.calls == [] and not actions.listing(idx).actions[0].done


def test_a_failed_comment_never_unsaves_a_written_label():
    idx, _ = _index()
    calls = []

    def host(method, url, token, body):
        calls.append(url.rsplit("/", 1)[1])
        if url.endswith("/comments"):
            raise RuntimeError("HTTP 502: Bad Gateway")
        return {}

    act = actions.tick(idx, 1, True, None, request=host)
    assert act.done and actions.listing(idx).actions[0].done
    assert calls == ["labels", "comments", "comments"]  # one retry, then logged


def test_comments_land_in_the_order_their_labels_were_accepted():
    idx, _ = _index()
    calls = []

    def host(method, url, token, body):
        calls.append((method, body and body.get("body", "")[:8]))
        return {}

    actions.tick(idx, 1, True, None, request=host, defer=actions.in_background)
    actions.tick(idx, 1, False, None, request=host, defer=actions.in_background)
    actions.wait_for_comments()
    assert [c for c in calls if c[0] == "POST" and c[1]] == [
        ("POST", "Marked d"), ("POST", "Unticked")]


def test_concurrent_ticks_on_different_rows_both_stick():
    idx, _ = _index(issues=[_issue(n) for n in range(1, 21)])
    host = FakeHost([])
    threads = [threading.Thread(target=actions.tick, args=(idx, n, True, None),
                                kwargs={"request": host}) for n in range(1, 21)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(a.done for a in actions.listing(idx).actions)


def _node(prog: str, *args: str) -> str:
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    assert node, "node is required to test console.js (preinstalled on CI runners)"
    script = Path(__file__).resolve().parent.parent / "console/static/console.js"
    return subprocess.run([node, "-e", prog, str(script), *args],
                          capture_output=True, text=True, check=True).stdout


def test_the_script_ticks_at_once_and_unticks_on_a_failed_save():
    """`tickFlow`: the box changes before anything is sent; a confirmed save
    confirms, a refusal or a dropped connection rolls back with the reason;
    several ticks can be in flight, and the page is re-read once, after the
    last one settles."""
    prog = r"""
const m = require(process.argv[1]);
const log = [];
const state = {inFlight: 0, onIdle: () => log.push("idle")};
function ui(name) {
  return {optimistic: () => log.push(name + ":ticked"),
          confirm: r => log.push(name + ":confirm:" + r.number),
          rollback: msg => log.push(name + ":rollback:" + msg)};
}
let resolveA;
const a = m.tickFlow(ui("a"), () => new Promise(r => { resolveA = r; }), state);
log.push("inFlight=" + state.inFlight);  // the box is ticked before the reply
const b = m.tickFlow(ui("b"), () => Promise.resolve({ok: false, error: "Not saved: refused"}), state);
const c = m.tickFlow(ui("c"), () => Promise.reject(new Error("network down")), state);
Promise.all([b, c]).then(() => {
  log.push("inFlight=" + state.inFlight);
  resolveA({ok: true, number: 1});
  return a;
}).then(() => console.log(JSON.stringify(log)));
"""
    log = json.loads(_node(prog))
    assert log[:2] == ["a:ticked", "inFlight=1"]
    assert log.index("b:ticked") < log.index("b:rollback:Not saved: refused")
    assert "c:rollback:not saved: network down" in log
    assert log[-3:] == ["inFlight=1", "a:confirm:1", "idle"]
    assert log.count("idle") == 1


def test_only_the_servers_json_verdict_confirms_a_tick():
    cases = [
        [200, "application/json; charset=utf-8", '{"ok": true, "number": 4}'],
        [404, "application/json", '{"ok": false, "error": "Not saved: gone"}'],
        # A login page or proxy error in place of the verdict is a failure.
        [200, "text/html", "<html>sign in</html>"],
        [502, "text/html", "<h1>502</h1>"],
        [200, "application/json", "not json"],
    ]
    prog = ("const m=require(process.argv[1]);"
            "console.log(JSON.stringify(JSON.parse(process.argv[2]).map(c=>m.tickReply(...c))))")
    out = json.loads(_node(prog, json.dumps(cases)))
    assert out[0] == {"ok": True, "number": 4}
    assert out[1] == {"ok": False, "error": "Not saved: gone"}
    assert all(o["ok"] is False and "did not confirm" in o["error"] for o in out[2:])
    assert "HTTP 502" in out[3]["error"]


def test_the_page_script_posts_ticks_for_json_and_falls_back_to_the_form():
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "console/static/console.js").read_text()
    assert 'document.addEventListener("submit"' in src
    assert '"Accept": "application/json"' in src
    # No script, or no fetch: the form posts as it always did.
    assert "if (!window.fetch || !window.URLSearchParams || !window.FormData) return;" in src
    # A cadence refresh never swaps the page out from under an unsaved tick
    # or a reader who is active (Brian 2026-11-06: the swap undid his clicks).
    assert "return editing() || ticks.inFlight > 0 || Date.now() - lastActive < QUIET_MS;" in src
    assert "if (busy()) { retrySoon(); return; }" in src
    # A settled tick moves its row and recounts in place; it never re-reads.
    assert "ticks.onIdle" not in src and "recountActions();" in src
    # Expanded rows survive a cadence swap.
    assert 'rows[r].classList.add("row-open")' in src


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


# ------------------------------------------------------- PRs and commands ---

def test_the_page_links_prs_and_shows_commands_never_the_issue():
    """Brian 2026-11-04: "why are issues linked? shouldn't this just be prs and commands?" """
    pr_body = ("Merge [nous-ergon-ops#2133](https://github.com/nousergon/nous-ergon-ops/pull/2133): "
               "Staging bucket policy.\n\nYour steps, one command (sign in as ne-admin):\n\n"
               "`aws s3 cp s3://b/run-x.sh ~/run-x.sh --profile ne-admin && bash ~/run-x.sh`\n\n"
               "<!-- merge-ask: nousergon/nous-ergon-ops#2133 -->")
    cmd_body = "Rotate the key.\n\n```\nbash ~/rotate.sh\n```"
    idx, _ = _index(issues=[_issue(8, "Merge nous-ergon-ops#2133", body=pr_body),
                            _issue(9, "Rotate the key", body=cmd_body)])
    html = _html(idx, resolve("/actions"))
    assert "https://example.test/8" not in html and "https://example.test/9" not in html
    assert 'href="https://github.com/nousergon/nous-ergon-ops/pull/2133"' in html
    assert ">merge nous-ergon-ops#2133</a>" in html
    assert "bash ~/run-x.sh</code></pre>" in html and "bash ~/rotate.sh</code></pre>" in html
    assert "tracker-I8" not in html.split('<table class="actions">')[1]
    doc = render_json.payload(idx, resolve("/actions"))
    by_n = {a["number"]: a for a in doc["actions"]}
    assert by_n[8]["pr_ref"] == "nous-ergon-ops#2133" and by_n[9]["command"] == "bash ~/rotate.sh"


# --- actionable only; everything else is the done archive (Brian 2026-11-05) --

def _read(issues, closed=(), prs=None):
    host = FakeHost(issues, closed, prs)
    found, _ = actions.read(actions.parse(DECL), "2026-11-05T12:00:00Z", request=host)
    return found, host


def test_an_action_whose_pr_has_merged_is_done_not_to_do():
    body = "Merge https://github.com/acme/atlas/pull/7 after review."
    found, host = _read(
        [_issue(1, "Merge atlas", body=body), _issue(2, "Run the key script")],
        prs={"/repos/acme/atlas/issues/7": {"state": "closed",
                                            "pull_request": {"merged_at": "2026-11-05T09:00:00Z"}}})
    assert [a.number for a in found.todo] == [2]
    assert [a.number for a in found.done] == [1]
    assert found.done[0].pr_state == "merged"
    # The PR's own repository is read with a token minted for that repository.
    assert any(url.endswith("/repos/acme/atlas/issues/7") for _, url, _, _ in host.calls)


def test_an_open_pr_stays_to_do_and_an_unreadable_one_is_not_archived_on_a_guess():
    found, _ = _read(
        [_issue(1, body="https://github.com/acme/a/pull/1"),
         _issue(2, body="https://github.com/acme/b/pull/2"),
         _issue(3, body="https://github.com/other/c/pull/3")],
        prs={"/repos/acme/a/issues/1": {"state": "open", "pull_request": {"merged_at": None}},
             "/repos/acme/b/issues/2": RuntimeError("HTTP 404: Not Found")})
    assert [a.number for a in found.todo] == [1, 2, 3]
    assert [a.pr_state for a in found.todo] == ["open", None, None]


def test_closed_actions_are_the_archive_newest_first_and_ticked_read_only():
    closed = [dict(_issue(8, "Old ask"), state="closed", closed_at="2026-11-01T00:00:00Z"),
              dict(_issue(9, "New ask"), state="closed", closed_at="2026-11-04T00:00:00Z")]
    found, _ = _read([_issue(1)], closed=closed)
    assert [a.number for a in found.archive] == [9, 8]
    assert [a.number for a in found.done] == [9, 8]
    idx = Index()
    actions.attach(idx, found)
    idx.finalize()
    html = _html(idx, resolve("/actions"))
    done = html.split("<h2>Done · 2</h2>", 1)[1]
    assert "New ask" in done and "closed 2026-11-04" in done
    # Closed: ticked, but not a form a reader could untick.
    assert 'action="/actions/9"' not in html and "☑" in done
    with pytest.raises(actions.TickRefused):
        actions.tick(idx, 9, False, "brian")


def test_a_failed_archive_read_never_fails_the_list():
    class Flaky(FakeHost):
        def __call__(self, method, url, token, body):
            if "state=closed" in url:
                raise RuntimeError("HTTP 502: Bad Gateway")
            return super().__call__(method, url, token, body)

    found, _ = actions.read(actions.parse(DECL), "2026-11-05T12:00:00Z",
                            request=Flaky([_issue(1)]))
    assert found.error is None and [a.number for a in found.todo] == [1]
    assert found.archive_error == "HTTP 502: Bad Gateway"


def test_a_related_pr_merging_does_not_end_a_command_ask():
    """aec#10471: its ask is two gcloud deletions; atlas#656 is only context."""
    body = ("Related PR: https://github.com/acme/atlas/pull/656\n\n"
            "```\ngcloud storage rm -r gs://x\n```\n")
    found, _ = _read(
        [_issue(1, "Mothballing atlas leaves GCP storage", body=body)],
        prs={"/repos/acme/atlas/issues/656": {"state": "closed",
                                              "pull_request": {"merged_at": "2026-09-10T00:00:00Z"}}})
    assert [a.number for a in found.todo] == [1]
    idx = Index()
    actions.attach(idx, found)
    idx.finalize()
    html = _html(idx, resolve("/actions"))
    assert "<strong>Mothballing atlas leaves GCP storage</strong>" in html
    assert 'Related <a href="https://github.com/acme/atlas/pull/656">atlas#656</a> (merged)' in html
    assert "gcloud storage rm -r gs://x</code></pre>" in html


def test_the_ask_lists_every_pr_to_merge_and_every_command_to_copy():
    """Brian 2026-11-05: each row says the PRs to merge, linked, and the full
    set of commands, copyable, when the row is opened. Evidence blocks are
    never offered as commands."""
    body = ("When: after https://github.com/acme/ops/pull/2172 merges\n\n"
            "Related PR: https://github.com/acme/ops/pull/2169\n\n"
            "```\naws s3api head-object --bucket b --key k\n  LastModified 2026-10-27\n```\n\n"
            "```\nbash a.sh && bash a.sh --apply\n```\n\n"
            "Then:\n\n`gh workflow run tofu-apply-on-merge.yml --ref main`\n")
    found, _ = _read([_issue(1, "Apply the staging role", body=body)],
                     prs={"/repos/acme/ops/issues/2172": {"state": "open", "pull_request": {}},
                          "/repos/acme/ops/issues/2169": {"state": "closed",
                                                         "pull_request": {"merged_at": "x"}}})
    a = found.todo[0]
    assert a.commands == ("bash a.sh && bash a.sh --apply",
                          "gh workflow run tofu-apply-on-merge.yml --ref main")
    idx = Index()
    actions.attach(idx, found)
    idx.finalize()
    html = _html(idx, resolve("/actions"))
    assert 'Merge <a href="https://github.com/acme/ops/pull/2172">ops#2172</a></li>' in html
    assert 'Related <a href="https://github.com/acme/ops/pull/2169">ops#2169</a> (merged)' in html
    assert "Run, in order:" in html and html.count('class="action-copy"') == 2
    assert "LastModified" not in html.split('class="action-run"')[1]
    assert "merge ops#2172</a> · run 2 commands" in html
    doc = render_json.payload(idx, resolve("/actions"))
    assert doc["actions"][0]["commands"][1].startswith("gh workflow run")
    assert {p["ref"]: p["state"] for p in doc["actions"][0]["prs"]} == {
        "ops#2172": "open", "ops#2169": "merged"}


def test_a_crlf_issue_body_still_yields_its_fenced_command():
    """I13239 (codex ee3c591006774d77): a body edited in a browser carries
    CRLF, and the fence regex required a bare LF after the language tag."""
    body = "Run this:\r\n```bash\r\naws s3 ls s3://bucket/prefix/\r\n```\r\n"
    assert actions.command(body) == "aws s3 ls s3://bucket/prefix/"
    assert actions.commands(body) == ("aws s3 ls s3://bucket/prefix/",)


# --- I13239 pass 3 (codex findings) ------------------------------------------
def _page(url):
    from urllib.parse import parse_qs, urlsplit

    return int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])


def test_a_when_bound_keeps_its_operators_and_says_when_it_is_cut():
    # codex 942684eb6a5de678
    _, _, when = actions.timing("When: **after** > 16:30 ET, once PR #12 lands")
    assert when == "after > 16:30 ET, once PR #12 lands"
    assert actions.timing("When: > 16:30 ET")[2] == "> 16:30 ET"
    _, _, when = actions.timing("When: " + "word " * 60)
    assert when.endswith("…") and len(when) <= actions.WHEN_CHARS + 1


def test_a_repeated_step_is_kept_and_an_immediate_repeat_is_not():
    # codex 88ec14d066fdbf57
    body = ("```\naws sso login\n```\n\n```\ngh pr merge 1\n```\n\n"
            "```\naws sso login\n```\n\n`aws sso login`\n")
    assert actions.commands(body) == ("aws sso login", "gh pr merge 1", "aws sso login")


def test_open_actions_beyond_one_page_are_read():
    # codex e978e70a0e90aa2c
    class Paged(FakeHost):
        def __call__(self, method, url, token, body):
            self.calls.append((method, url, token, body))
            if "state=closed" in url:
                return []
            if _page(url) == 1:
                return [_issue(n) for n in range(1, actions.MAX_ACTIONS + 1)]
            if _page(url) == 2:
                return [_issue(actions.MAX_ACTIONS + 1)]
            return []

    found, _ = actions.read(actions.parse(DECL), "2026-11-05T12:00:00Z", request=Paged([]))
    assert len(found.actions) == actions.MAX_ACTIONS + 1 and not found.truncated


def test_a_read_that_hits_the_page_cap_says_so():
    class Endless(FakeHost):
        def __call__(self, method, url, token, body):
            if "state=closed" in url:
                return []
            base = (_page(url) - 1) * actions.MAX_ACTIONS
            return [_issue(base + n) for n in range(1, actions.MAX_ACTIONS + 1)]

    found, _ = actions.read(actions.parse(DECL), "2026-11-05T12:00:00Z", request=Endless([]))
    assert found.truncated and actions.as_json(found)["truncated"] is True
    idx = Index()
    actions.attach(idx, found)
    idx.finalize()
    assert "the rest are on the tracker only" in _html(idx, resolve("/actions"))


def test_the_archive_is_the_last_closed_not_the_last_updated():
    # codex eb6b5e9f7f84f623: 100 old closures touched recently fill page 1;
    # the newest closure is on page 2 and must still lead the archive.
    def closed(n, closed_at, updated):
        return dict(_issue(n), state="closed", closed_at=closed_at, updated_at=updated)

    class Paged(FakeHost):
        def __call__(self, method, url, token, body):
            if "state=closed" in url:
                if _page(url) == 1:
                    return [closed(100 + n, "2026-10-01T00:00:00Z", "2026-11-05T00:00:00Z")
                            for n in range(actions.MAX_ACTIONS)]
                if _page(url) == 2:
                    return [closed(7, "2026-11-04T00:00:00Z", "2026-11-04T00:00:00Z")]
                return []
            return super().__call__(method, url, token, body)

    found, _ = actions.read(actions.parse(DECL), "2026-11-05T12:00:00Z",
                            request=Paged([_issue(1)]))
    assert found.archive[0].number == 7 and len(found.archive) == actions.ARCHIVE_SIZE


def test_the_actions_page_says_how_current_it_is():
    # codex 859c65deadf22b93
    idx, _ = _index()
    assert "index build time unknown" in _html(idx, resolve("/actions"))
