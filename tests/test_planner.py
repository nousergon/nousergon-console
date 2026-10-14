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
    assert plan.projects[0].alerts and plan.projects[0].alerts.sources


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


def test_pace_false_judges_a_gate_read_on_the_day_by_date_and_blockers():
    """A milestone whose items all close at the target reads 0/N until then on
    a perfect week; `pace: false` stops that reading as THREATENED."""
    reqs = ["repo-I4", "repo-I4", "repo-I4"]
    paced = _one(_plan([_ms(["repo-I1", "repo-I4", "repo-I3"],
                            target="2026-10-31")]), date(2026, 10, 25))
    assert paced["state"] in (planner.THREATENED, planner.OFF_TRACK)
    unpaced = _one(_plan([_ms(["repo-I1", "repo-I4", "repo-I3"], target="2026-10-31",
                              pace=False)]), date(2026, 10, 20))
    assert unpaced["state"] == planner.ON_TRACK and "not pace" in unpaced["reason"]
    blocked = _one(_plan([_ms(["repo-I3", "repo-I4"], target="2026-10-31",
                              pace=False)]), date(2026, 10, 28))
    assert blocked["state"] == planner.THREATENED
    with pytest.raises(planner.PlannerConfigError, match="pace"):
        _plan([_ms(reqs, pace="no")])


# ------------------------------------------------------------ alert updates --

def _alert_row(eid, kind="incident", facets=None, as_of="2026-10-13T10:00:00Z",
               evidence="s3://b/k.json", **fields):
    from console.model.entity import Entity, Provenance
    from console.model.kinds import Kind

    return Entity(kind=Kind(kind), id=eid, state="open",
                  provenance=Provenance(source="s3://b", as_of=as_of, evidence=evidence),
                  facets=facets if facets is not None else {"pipeline": "pages"},
                  detail={"fields": {k: {"value": v, "render": "text"}
                                     for k, v in fields.items()}})


def _alert_index(plan, rows):
    from console.model.envelope import AdapterResult

    index = _index(plan)
    index.add_result(AdapterResult(name="pages", status=AdapterStatus.OK,
                                   entities=tuple(rows)))
    return index


def _alert_plan(alerts):
    return planner.parse({"org": "o", "projects": [
        {"id": "p", "title": "Project", "milestones": [_ms(["repo-I1"])],
         "alerts": alerts}]})


PAGES = {"name": "page", "kind": "incident", "facets": {"pipeline": "pages"},
         "text": "{rendered}", "at": "last_observed_utc", "where": {"sent": True},
         "exclude": {"synthetic": True}}


def test_alerts_list_newest_first_with_text_time_and_link():
    rows = [
        _alert_row("a-old", rendered="[crucible-v2] FAILURE old\ncause: x",
                   last_observed_utc="2026-10-12T01:00:00Z", sent=True),
        _alert_row("a-new", rendered="[crucible-v2] FAILURE new",
                   last_observed_utc="2026-10-14T01:02:23Z", sent=True),
        _alert_row("a-unsent", rendered="never sent",
                   last_observed_utc="2026-10-15T00:00:00Z", sent=False),
        _alert_row("other", facets={"pipeline": "elsewhere"}, rendered="not mine",
                   sent=True),
        _alert_row("drill", rendered="drill", sent=True, synthetic=True,
                   last_observed_utc="2026-10-16T00:00:00Z"),
    ]
    a = planner.evaluate(_alert_index(_alert_plan({"sources": [PAGES]}), rows),
                         now=date(2026, 10, 14))[0]["alerts"]
    assert [i["entity"] for i in a["items"]] == ["a-new", "a-old"]
    first, second = a["items"]
    assert first["at"] == "2026-10-14T01:02:23Z"
    assert second["text"] == "[crucible-v2] FAILURE old"  # first line only
    # evidence is not http(s), so the link is the row's own console page
    assert first["url"] == first["entity_url"] == "/incident/a-new"
    assert a["sources"] == [{"name": "page", "selector":
                             "kind=incident pipeline=pages sent=True synthetic!=True",
                             "matched": 2}]


def test_alerts_limit_link_field_and_as_of_fallback():
    report = {"name": "daily", "facets": {"pipeline": "pages"},
              "text": "{phase}: board {board_met}", "link": "update_url"}
    rows = [_alert_row(f"r{n}", as_of=f"2026-10-1{n}T15:00:00Z", phase="p1",
                       board_met=f"{n}/9", update_url=f"https://gh/c{n}")
            for n in range(1, 5)]
    rows.append(_alert_row("r0", as_of=None, phase=None))
    a = planner.evaluate(_alert_index(
        _alert_plan({"limit": 2, "sources": [report]}), rows))[0]["alerts"]
    assert [i["entity"] for i in a["items"]] == ["r4", "r3"]
    assert a["items"][0]["text"] == "p1: board 4/9"
    assert a["items"][0]["url"] == "https://gh/c4"
    assert a["limit"] == 2 and a["sources"][0]["matched"] == 5


def test_a_template_naming_a_missing_field_falls_back_to_the_row_id():
    src = {"name": "x", "facets": {"pipeline": "pages"}, "text": "{nope}"}
    a = planner.evaluate(_alert_index(_alert_plan({"sources": [src]}),
                                      [_alert_row("row-1")]))[0]["alerts"]
    assert a["items"][0]["text"] == "row-1"


def test_undeclared_and_unmatched_alerts_render_their_absence():
    from console.render.html import planner_page, planner_project_page

    none = _index(_plan([_ms(["repo-I1"])]))
    assert planner.evaluate(none)[0]["alerts"]["declared"] is False
    assert "no alert source declared" in planner_page(none)

    index = _alert_index(_alert_plan({"sources": [PAGES]}), [])
    page = planner_project_page(index, "p")
    assert "alert updates (latest 5)" in page
    assert "page: no row matches" in page and "pipeline=pages" in page


def test_alerts_render_on_both_pages_and_in_json():
    from console.render.html import planner_page, planner_project_page

    rows = [_alert_row("a1", rendered="<b>FAILURE</b>", sent=True,
                       last_observed_utc="2026-10-14T01:00:00Z",
                       evidence="https://example/a1")]
    index = _alert_index(_alert_plan({"sources": [PAGES]}), rows)
    for page in (planner_page(index), planner_project_page(index, "p")):
        assert "&lt;b&gt;FAILURE&lt;/b&gt;" in page and "<b>FAILURE" not in page
        assert 'href="https://example/a1"' in page and 'href="/incident/a1"' in page
    doc = render_json.payload(index, resolve("/planner"))
    assert doc["projects"][0]["alerts"]["items"][0]["entity"] == "a1"


@pytest.mark.parametrize("alerts, needle", [
    ({"sources": []}, "at least one selector"),
    ({"sources": [{"name": "x"}]}, "facets"),
    ({"sources": [{"name": "x", "facets": {}}]}, "facets"),
    ({"sources": [{"name": "x", "facets": {"a": "b"}, "kind": "nope"}]}, "kind"),
    ({"sources": [{"name": "x", "facets": {"a": "b"}},
                  {"name": "x", "facets": {"a": "c"}}]}, "twice"),
    ({"limit": 0, "sources": [{"name": "x", "facets": {"a": "b"}}]}, "limit"),
    ({"sources": [{"name": "x", "facets": {"a": "b"}, "text": "{oops"}]}, "template"),
    ({"sources": [{"name": "x", "facets": {"a": "b"}, "exclude": ["s"]}]}, "exclude"),
    (["not", "a", "mapping"], "mapping"),
])
def test_bad_alert_declarations_fail_by_name(alerts, needle):
    with pytest.raises(planner.PlannerConfigError, match=needle):
        _alert_plan(alerts)


def test_equal_instants_break_on_id_descending():
    src = {"name": "x", "facets": {"pipeline": "pages"}}
    rows = [_alert_row(f"day:2026-10-0{n}") for n in (2, 7, 4)]
    a = planner.evaluate(_alert_index(_alert_plan({"sources": [src]}), rows))[0]["alerts"]
    assert [i["entity"] for i in a["items"]] == [
        "day:2026-10-07", "day:2026-10-04", "day:2026-10-02"]
