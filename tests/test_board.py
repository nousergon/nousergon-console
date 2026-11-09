"""Planning board (Brian 2026-11-09) — ranked projects, live epic issues,
Brian's dates and the complete checkbox saved to S3, and the archive."""
from __future__ import annotations

import http.client
import json
import threading
from datetime import date
from typing import Any

import pytest

from console.adapters import git_host
from console.index import board, planner
from console.index.graph import Index
from console.render import html as render_html
from console.render import json as render_json
from console.server.app import serve
from console.server.router import resolve

STORE = "s3://alpha-engine-research/dashboard/planning/board.json"

ISSUES = {
    4: {"number": 4, "title": "named open", "state": "OPEN",
        "labels": [{"name": "in-progress"}], "url": "https://example/4", "blockedBy": []},
    5: {"number": 5, "title": "named closed", "state": "CLOSED", "labels": [],
        "url": "https://example/5", "blockedBy": []},
}

SUBS = {
    10: [{"number": 11, "title": "sub open", "state": "open", "labels": [],
          "html_url": "https://example/11",
          "repository_url": "https://api.github.com/repos/o/repo"},
         {"number": 12, "title": "sub gated", "state": "open",
          "labels": [{"name": "gate:decision"}], "html_url": "https://example/12"},
         {"number": 13, "title": "sub done", "state": "closed", "labels": []}],
}


class FakeS3:
    def __init__(self, body: bytes | None = None) -> None:
        self.body = body
        self.puts: list[bytes] = []

    def get(self, bucket: str, key: str) -> bytes | None:
        return self.body

    def put(self, bucket: str, key: str, body: bytes) -> None:
        self.body = body
        self.puts.append(body)


def _sub_reader(org: str, repo: str, number: int, cache: str) -> list[dict[str, Any]]:
    if number not in SUBS:
        raise RuntimeError("HTTP 404: Not Found")
    return SUBS[number]


def _raw() -> dict[str, Any]:
    return {"org": "o", "board": {"store": STORE}, "projects": [
        {"id": "alpha", "title": "Alpha", "epics": ["repo-I10"], "milestones": [
            {"id": "m1", "title": "M1", "start": "2026-11-01", "target": "2026-11-20",
             "requires": ["repo-I5"]},
            {"id": "m2", "title": "M2", "start": "2026-11-01", "target": "2026-12-15",
             "requires": ["repo-I4"]}]},
        {"id": "beta", "title": "Beta", "issues": ["repo-I4", "repo-I5"]},
        {"id": "gamma", "title": "Gamma", "epics": ["repo-I99"]},
    ]}


def _index(s3: FakeS3) -> Index:
    raw = _raw()
    plan = planner.parse(raw)
    idx = Index()
    idx.add_result(git_host.fetch_refs(
        {"org": "o"}, plan.refs,
        ref_reader=lambda o, r, ns, c: [ISSUES[n] for n in ns if n in ISSUES]))
    rows, _ = git_host.fetch_sub_issues({"org": "o"}, plan.epics, reader=_sub_reader)
    planner.attach(idx, plan)
    found, _ = board.read(board.parse(raw), "2026-11-09T16:00:00Z", getter=s3.get)
    board.attach(idx, found, board.Epics(
        rows=rows, unread=tuple(e for e in plan.epics if e not in rows)))
    return idx


def _save(idx: Index, s3: FakeS3, pid: str, **form: str) -> dict[str, Any]:
    return board.save(idx, pid, form, "brian@example", "2026-11-09T16:05:00Z",
                      getter=s3.get, putter=s3.put)


def _order(idx: Index) -> list[str]:
    return [r["id"] for r in board.evaluate(idx, date(2026, 11, 9))["active"]]


# ------------------------------------------------------------- declaration --

@pytest.mark.parametrize("raw, message", [
    ({"board": "x"}, "mapping"),
    ({"board": {"store": "https://x/y.json"}}, "s3://"),
    ({"board": {"store": STORE, "extra": 1}}, "unknown key"),
])
def test_a_bad_board_block_fails_by_name(raw: dict[str, Any], message: str) -> None:
    with pytest.raises(board.BoardConfigError, match=message):
        board.parse(raw)


def test_no_board_block_means_no_board() -> None:
    assert board.parse({"projects": []}) is None
    assert render_html._planning_board(Index()) == ("", "")  # pyright: ignore[reportPrivateUsage]


def test_epics_and_issues_take_tracker_refs_only() -> None:
    with pytest.raises(planner.PlannerConfigError, match="epics"):
        planner.parse({"projects": [{"id": "p", "title": "P", "epics": ["ops#12"]}]})
    with pytest.raises(planner.PlannerConfigError, match="twice"):
        planner.parse({"projects": [{"id": "p", "title": "P", "issues": ["r-I1", "r-I1"]}]})


def test_named_issues_join_the_refs_read_by_name() -> None:
    plan = planner.parse(_raw())
    assert "repo-I4" in plan.refs and plan.epics == ("repo-I10", "repo-I99")


# -------------------------------------------------------------- evaluation --

def test_rows_list_open_epic_and_named_issues_in_progress_first() -> None:
    b = board.evaluate(_index(FakeS3()), date(2026, 11, 9))
    alpha = b["active"][0]
    assert [i["ref"] for i in alpha["open"]] == ["repo-I4", "repo-I12", "repo-I11"]
    assert alpha["open"][0]["in_progress"] and alpha["open"][1]["blocker"] == "gated: gate:decision"
    assert (alpha["n_open"], alpha["n_closed"]) == (3, 2)
    assert (alpha["plan_next"], alpha["plan_final"]) == ("2026-12-15", "2026-12-15")


def test_an_unreadable_epic_is_named_never_empty() -> None:
    b = board.evaluate(_index(FakeS3()), date(2026, 11, 9))
    gamma = next(r for r in b["active"] if r["id"] == "gamma")
    assert gamma["unread_epics"] == ["repo-I99"] and b["unread_epics"] == ["repo-I99"]
    assert "cannot read repo-I99" in render_html.planner_page(_index(FakeS3()))


def test_unranked_projects_follow_declared_order() -> None:
    assert _order(_index(FakeS3())) == ["alpha", "beta", "gamma"]


# ------------------------------------------------------------------- write --

def test_a_rank_moves_the_row_and_renumbers_every_active_project() -> None:
    s3 = FakeS3()
    idx = _index(s3)
    _save(idx, s3, "gamma", rank="1", next_milestone="2026-11-20", final="2027-01-31")
    assert _order(idx) == ["gamma", "alpha", "beta"]
    doc = json.loads(s3.body or b"{}")
    assert {k: v["rank"] for k, v in doc["projects"].items()} == {"gamma": 1, "alpha": 2, "beta": 3}
    assert doc["projects"]["gamma"]["next_milestone"] == "2026-11-20"
    # Saving without a rank keeps the row where it is.
    _save(idx, s3, "alpha", final="2026-12-31")
    assert _order(idx) == ["gamma", "alpha", "beta"]


def test_complete_moves_a_project_to_the_archive_and_reopen_returns_it() -> None:
    s3 = FakeS3()
    idx = _index(s3)
    _save(idx, s3, "alpha", complete="1", final="2026-12-15")
    b = board.evaluate(idx, date(2026, 11, 9))
    assert [r["id"] for r in b["active"]] == ["beta", "gamma"]
    assert [r["id"] for r in b["archive"]] == ["alpha"]
    assert b["archive"][0]["completed_by"] == "brian@example"
    page = render_html.planner_page(idx)
    assert page.index("archive") > page.index("calendar")  # archive is at the bottom
    _save(idx, s3, "alpha")
    assert _order(idx) == ["beta", "gamma", "alpha"]


def test_a_past_date_on_an_active_project_reads_overdue() -> None:
    s3 = FakeS3()
    idx = _index(s3)
    _save(idx, s3, "beta", next_milestone="2026-11-01")
    beta = next(r for r in board.evaluate(idx, date(2026, 11, 9))["active"] if r["id"] == "beta")
    assert beta["overdue"] == ["next milestone"]


@pytest.mark.parametrize("pid, form, status", [
    ("nope", {}, 404),
    ("alpha", {"rank": "first"}, 400),
    ("alpha", {"next_milestone": "soon"}, 400),
    ("alpha", {"final": "1999-01-01"}, 400),
])
def test_a_bad_edit_is_refused_and_nothing_is_written(pid: str, form: dict[str, str],
                                                      status: int) -> None:
    s3 = FakeS3()
    with pytest.raises(board.SaveRefused) as exc:
        _save(_index(s3), s3, pid, **form)
    assert exc.value.status == status and s3.puts == []


def test_an_unreadable_store_renders_read_only() -> None:
    s3 = FakeS3(b"not json")
    idx = _index(s3)
    b = board.evaluate(idx)
    assert not b["writable"] and b["error"]
    assert "read-only" in render_html.planner_page(idx)


def test_the_planner_json_carries_the_board() -> None:
    doc = render_json.payload(_index(FakeS3()), resolve("/planner", ""))
    assert [r["id"] for r in doc["board"]["active"]] == ["alpha", "beta", "gamma"]


# ---------------------------------------------------------- sub-issue read --

def test_sub_issues_page_until_a_short_page() -> None:
    calls: list[str] = []

    def get_json(path: str) -> Any:
        calls.append(path)
        return [{"number": n, "state": "open"} for n in range(100 if path.endswith("&page=1") else 3)]

    rows = git_host._sub_issues(get_json, "o", "repo", 7)  # pyright: ignore[reportPrivateUsage]
    assert len(rows) == 103 and len(calls) == 2


def test_an_epic_read_failure_is_unavailable_with_its_reason() -> None:
    rows, result = git_host.fetch_sub_issues({"org": "o"}, ["repo-I10", "repo-I99"],
                                             reader=_sub_reader)
    assert set(rows) == {"repo-I10"}
    assert any("repo-I99" in u and "404" in u for u in result.unavailable)


# ------------------------------------------------------------------- POST --

@pytest.fixture()
def live(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    s3 = FakeS3()
    idx = _index(s3)
    real = board.save
    monkeypatch.setattr(board, "save", lambda index, pid, form, actor, now, **_:
                        real(index, pid, form, actor, now, getter=s3.get, putter=s3.put))
    server = serve(idx, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], s3
    server.shutdown()
    server.server_close()


def _post(port: int, path: str, body: str,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], str]:
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request("POST", path, body=body,
                 headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})
    resp = conn.getresponse()
    out = resp.status, dict(resp.getheaders()), resp.read().decode()
    conn.close()
    return out


def test_a_same_origin_save_returns_to_the_row(live: tuple[int, FakeS3]) -> None:
    port, s3 = live
    status, headers, _ = _post(port, "/planner/beta", "rank=1&next_milestone=2026-11-30",
                               {"Origin": f"http://127.0.0.1:{port}"})
    assert status == 303 and headers["Location"] == "/planner#p-beta" and len(s3.puts) == 1


def test_a_cross_site_save_is_refused(live: tuple[int, FakeS3]) -> None:
    port, s3 = live
    status, _, _ = _post(port, "/planner/beta", "rank=1", {"Origin": "https://evil.test"})
    assert status == 403 and s3.puts == []


# ------------------------------------------------------------ sweep feed --

def test_status_and_sweep_order_lift_projects_that_are_behind() -> None:
    from datetime import datetime, timezone

    s3 = FakeS3()
    idx = _index(s3)
    # gamma ranked last but past its board date: off track, lifted 3 places.
    _save(idx, s3, "gamma", next_milestone="2026-11-01")
    b = board.evaluate(idx, now=datetime(2026, 11, 9, 12, tzinfo=timezone.utc))
    status = {r["id"]: r["status"] for r in b["active"]}
    assert status == {"alpha": "on track", "beta": "no dates", "gamma": "off track"}
    assert b["sweep_order"] == ["gamma", "alpha", "beta"]
    assert [r["position"] for r in b["active"]] == [1, 2, 3]  # rank is untouched
    assert "next milestone date passed" in b["active"][2]["status_reason"]


def test_issues_closed_in_the_last_day_show_what_the_sweep_did() -> None:
    from datetime import datetime, timezone

    SUBS[10][2]["closed_at"] = "2026-11-09T06:00:00Z"
    try:
        idx = _index(FakeS3())
        b = board.evaluate(idx, now=datetime(2026, 11, 9, 12, tzinfo=timezone.utc))
        assert [i["ref"] for i in b["active"][0]["closed_recent"]] == ["repo-I13"]
        b = board.evaluate(idx, now=datetime(2026, 11, 11, 12, tzinfo=timezone.utc))
        assert b["active"][0]["closed_recent"] == []
    finally:
        SUBS[10][2].pop("closed_at")
