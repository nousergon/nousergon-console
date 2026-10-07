"""Delivery planner (`alpha-engine-config-I12152`) — declarations, the
schedule-state rule, the named-ref read, and both representations."""
from __future__ import annotations

from datetime import date

import pytest

from console.adapters import git_host
from console.index import planner
from console.index.graph import Index
from console.model.envelope import AdapterStatus
from console.render import json as render_json
from console.server.router import UnknownRoute, resolve

ISSUES = {
    1: {"number": 1, "title": "done thing", "state": "CLOSED", "labels": [],
        "updatedAt": "2026-10-01T00:00:00Z", "closedAt": "2026-10-01T00:00:00Z",
        "url": "https://example/1", "blockedBy": []},
    2: {"number": 2, "title": "needs a ruling", "state": "OPEN",
        "labels": [{"name": "gate:decision"}], "updatedAt": "2026-10-02T00:00:00Z",
        "closedAt": None, "url": "https://example/2", "blockedBy": []},
    3: {"number": 3, "title": "waits on 9", "state": "OPEN", "labels": [],
        "updatedAt": "2026-10-02T00:00:00Z", "closedAt": None,
        "url": "https://example/3", "blockedBy": ["repo-I9"]},
    4: {"number": 4, "title": "plain open", "state": "OPEN", "labels": [],
        "updatedAt": "2026-10-02T00:00:00Z", "closedAt": None,
        "url": "https://example/4", "blockedBy": []},
}


def _reader(org, repo, numbers, cache):
    return [ISSUES[n] for n in numbers if n in ISSUES]


def _plan(milestones):
    return planner.parse({"org": "o", "projects": [
        {"id": "p", "title": "Project", "milestones": milestones}]})


def _index(plan):
    index = Index()
    index.add_result(git_host.fetch_refs({"org": "o"}, plan.refs, ref_reader=_reader))
    planner.attach(index, plan)
    return index


def _one(plan, today):
    return planner.evaluate(_index(plan), now=today)[0]["milestones"][0]


def _ms(requires, start="2026-10-01", target="2026-10-31", **kw):
    return {"id": "m", "title": "M", "start": start, "target": target,
            "requires": requires, **kw}


def test_all_closed_is_met():
    m = _one(_plan([_ms(["repo-I1"])]), date(2026, 10, 5))
    assert m["state"] == planner.MET and m["closed"] == 1 and m["of"] == 1


def test_on_track_when_work_keeps_pace_with_the_calendar():
    # 1 of 2 closed (50%) with 4 of 30 days elapsed.
    m = _one(_plan([_ms(["repo-I1", "repo-I4"])]), date(2026, 10, 5))
    assert m["state"] == planner.ON_TRACK
    assert "1 of 2 items closed (50%)" in m["reason"]


def test_threatened_when_behind_by_more_than_the_threatened_gap():
    # 1 of 3 closed (33%) with 15 of 30 days elapsed (50%): 17 points behind.
    m = _one(_plan([_ms(["repo-I1", "repo-I4", "repo-I3"])]), date(2026, 10, 16))
    assert m["state"] == planner.THREATENED


def test_off_track_when_far_behind():
    # 1 of 4 closed (25%) with 21 of 30 days elapsed (70%): 45 points behind.
    m = _one(_plan([_ms(["repo-I1", "repo-I4", "repo-I3", "repo-I2"])]),
             date(2026, 10, 22))
    assert m["state"] == planner.OFF_TRACK and "replan" in m["reason"]


def test_a_small_milestone_is_judged_on_date_and_blockers_not_pace():
    # One open issue is 0% done every day until it closes; pace would turn it
    # red a third of the way in.
    m = _one(_plan([_ms(["repo-I4"])]), date(2026, 10, 29))
    assert m["state"] == planner.ON_TRACK and "not pace" in m["reason"]
    m = _one(_plan([_ms(["repo-I4"])]), date(2026, 11, 1))
    assert m["state"] == planner.OFF_TRACK


def test_off_track_once_the_target_passes_unmet():
    m = _one(_plan([_ms(["repo-I1", "repo-I4"])]), date(2026, 11, 2))
    assert m["state"] == planner.OFF_TRACK
    assert "passed 2 days ago" in m["reason"]


def test_a_gated_item_near_the_target_threatens_it():
    # Pace is fine (2 of 3 closed? no: 1 of 2), but a human-gated item sits
    # open five days out.
    m = _one(_plan([_ms(["repo-I1", "repo-I2"], start="2026-10-25")]),
             date(2026, 10, 26))
    assert m["state"] == planner.THREATENED
    assert m["blockers"] == ["repo-I2"] and "gate:decision" in m["reason"]


def test_a_blocked_by_dependency_is_a_blocker():
    m = _one(_plan([_ms(["repo-I1", "repo-I3"], start="2026-10-25")]),
             date(2026, 10, 26))
    assert m["blockers"] == ["repo-I3"] and "blocked by repo-I9" in m["reason"]


def test_an_unreadable_ref_is_unreported_never_on_track():
    m = _one(_plan([_ms(["repo-I1", "repo-I77"])]), date(2026, 10, 5))
    assert m["state"] == planner.UNREPORTED and "repo-I77" in m["reason"]


def test_nothing_to_measure_is_unreported():
    m = _one(_plan([_ms([])]), date(2026, 10, 5))
    assert m["state"] == planner.UNREPORTED


def test_items_list_open_and_blocked_first():
    m = _one(_plan([_ms(["repo-I1", "repo-I4", "repo-I2"])]), date(2026, 10, 5))
    assert [i["ref"] for i in m["items"]] == ["repo-I2", "repo-I4", "repo-I1"]


def test_project_carries_counts_never_one_verdict():
    plan = _plan([_ms(["repo-I1"]), {**_ms(["repo-I4"]), "id": "m2"}])
    p = planner.evaluate(_index(plan), now=date(2026, 11, 2))[0]
    assert p["states"][planner.MET] == 1 and p["states"][planner.OFF_TRACK] == 1
    assert "state" not in p


@pytest.mark.parametrize("bad, needle", [
    ({"projects": [{"id": "p", "title": "P", "milestones": [
        _ms(["repo-I1"], target="2026-09-01")]}]}, "after `start`"),
    ({"projects": [{"id": "p", "title": "P", "milestones": [
        _ms(["not a ref"])]}]}, "tracker refs"),
    ({"projects": [{"id": "p", "title": "P", "milestones": [
        _ms(["repo-I1"], start="soon")]}]}, "YYYY-MM-DD"),
    ({"projects": [{"id": "p", "title": "P"}, {"id": "p", "title": "Q"}]}, "twice"),
    ({"thresholds": {"threatened_gap": 0.5, "off_track_gap": 0.4}}, "thresholds"),
])
def test_bad_declarations_fail_by_name(bad, needle):
    with pytest.raises(planner.PlannerConfigError, match=needle.replace("`", ".")):
        planner.parse(bad)


def test_fetch_refs_reads_named_issues_and_reports_the_missing():
    result = git_host.fetch_refs({"org": "o"}, ["repo-I3", "repo-I77"], ref_reader=_reader)
    assert result.status is AdapterStatus.OK
    (ent,) = result.entities
    assert ent.id == "repo-I3" and ent.detail["blocked_by"] == ["repo-I9"]
    assert result.unavailable == ("repo-I77",)


def test_fetch_refs_without_an_org_reaches_no_host():
    def boom(*a):
        raise AssertionError("must not be called")

    result = git_host.fetch_refs({}, ["repo-I1"], ref_reader=boom)
    assert result.status is AdapterStatus.FAILED


def test_routes_resolve():
    assert resolve("/planner").view == "planner"
    req = resolve("/planner/data-collector")
    assert req.view == "planner-project" and req.project_id == "data-collector"
    with pytest.raises(UnknownRoute):
        resolve("/planner/a/b")


def test_both_representations():
    from console.server.app import _html

    index = _index(_plan([_ms(["repo-I1", "repo-I4"])]))
    for path in ("/planner", "/planner/p"):
        req = resolve(path)
        doc = render_json.payload(index, req)
        assert doc["view"] == req.view
        assert doc["question"] == planner.PLANNER_QUESTION
        html = _html(index, req)
        assert html.startswith("<!doctype html>")
        assert planner.PLANNER_QUESTION.replace("'", "&#x27;") in html
        assert "schedule-" in html
    assert "repo-I4" in _html(index, resolve("/planner/p"))


def test_empty_plan_renders_its_absence():
    from console.server.app import _html

    html = _html(Index(), resolve("/planner"))
    assert "no projects declared" in html


def test_example_config_declares_a_plan_that_builds():
    import yaml
    from pathlib import Path

    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent
                          / "config.example.yaml").read_text())
    plan = planner.parse(cfg["planner"])
    assert plan.refs and plan.org is None


def test_calendar_colours_each_project_and_keeps_the_state_word():
    """Brian 2026-10-07: the calendar is colour-coded by project. The colour
    follows the project's declared position, so its own page draws it the
    same; the schedule state stays a word beside its diamond (§5.7)."""
    from console.render.html import PLANNER_PROJECT_COLOURS, planner_page, planner_project_page

    plan = planner.parse({"org": "o", "projects": [
        {"id": "a", "title": "Alpha", "milestones": [_ms(["repo-I1"], id="a1")]},
        {"id": "b", "title": "Beta", "milestones": [_ms(["repo-I4"], id="b1")]}]})
    index = _index(plan)
    page = planner_page(index)
    assert "<svg" in page
    assert PLANNER_PROJECT_COLOURS[0] in page and PLANNER_PROJECT_COLOURS[1] in page
    assert ">MET</tspan>" in page and ">ON_TRACK</tspan>" in page
    beta = planner_project_page(index, "b")
    assert PLANNER_PROJECT_COLOURS[1] in beta and PLANNER_PROJECT_COLOURS[0] not in beta
