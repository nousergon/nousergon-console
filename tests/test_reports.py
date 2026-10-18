"""Report feeds (`/reports`) — declarations, the selector over rows the
s3-records adapter already mints, the recent window vs the dated archive, safe
rendering, absence, and both representations."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from console.adapters import s3_records
from console.index import reports
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import json as render_json
from console.render.panes import PANES
from console.server.router import UnknownRoute, resolve

NOW = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)
PREFIX = "ops/daily/"

#: Twenty mornings of reports, one per day, plus keys the feed must not take.
DAYS = [f"2026-09-{d:02d}" for d in range(27, 31)] + [
    f"2026-10-{d:02d}" for d in range(1, 16)]
OBJECTS = [(f"{PREFIX}{d}.md", f"{d}T13:30:00+00:00") for d in DAYS] + [
    (f"{PREFIX}merged-2026-10-15.md", "2026-10-15T13:05:00+00:00"),
    (f"{PREFIX}run-2026-10-15.started", "2026-10-15T02:00:00+00:00"),
    (f"{PREFIX}deadline-asks.json", "2026-10-15T13:00:00+00:00"),
]

ADAPTER = {
    "_name": "daily-report", "bucket": "b", "prefix": PREFIX, "kind": "run",
    "format": "object", "body": "markdown",
    "key_pattern": r"^ops/daily/(?P<cycle>\d{4}-\d{2}-\d{2})\.md$",
    "id_template": "report:{cycle}:daily",
    "date_fields": {"day": {"path": "cycle"}},
    "label_template": "{day:%a %-m/%-d} daily report",
    "as_of_field": "last_modified", "state_default": "HEALTHY",
    "facets": {"pane": {"value": "reports"}, "report": {"value": "daily"}},
}

FEEDS = {"feeds": [{"id": "daily", "title": "Daily report",
                    "kind": "run", "facets": {"report": "daily"},
                    "recent_days": 14, "archive": "s3://b/ops/daily/YYYY-MM-DD.md"}]}


def _text(bucket, key):
    day = key.rsplit("/", 1)[1][:-3]
    return (f"# Report {day}\n\nAll **green**. <script>alert(1)</script>\n\n"
            f"[bad](javascript:alert(1)) [good](https://example.com/{day})\n")


def _index(feeds=FEEDS, objects=OBJECTS, text_reader=_text):
    index = Index()
    index.add_result(s3_records.fetch(
        ADAPTER, lister=lambda b, p: list(objects),
        reader=lambda b, k: pytest.fail("format: object reads no parsed body"),
        text_reader=text_reader, now=NOW))
    reports.attach(index, reports.parse(feeds))
    index.finalize()
    return index


def _feed(index):
    return reports.evaluate(index, reports.feed(index, None), now=NOW)


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    """The pages read the window against the wall clock; the fixtures are
    dated around NOW. Unpinned, the page tests passed only until the recent
    window slid past the fixture dates (they failed from 2026-10-18)."""
    evaluate = reports.evaluate

    def at_now(index, f, now=None):
        return evaluate(index, f, now=now or NOW)

    monkeypatch.setattr(reports, "evaluate", at_now)


# ------------------------------------------------------------- selection --

def test_newest_report_first_recent_window_then_dated_archive():
    f = _feed(_index())
    assert f["matched"] == len(DAYS)  # merged-, run-, json keys never selected
    assert f["latest"]["id"] == "report:2026-10-15:daily"
    assert f["latest"]["label"] == "Thu 10/15 daily report"
    assert "# Report 2026-10-15" in f["latest"]["document"]["text"]
    recent = [r["id"] for r in f["recent"]]
    # The 14 days before NOW, newest first, after the one shown in full.
    assert recent[0] == "report:2026-10-14:daily"
    assert recent[-1] == "report:2026-10-03:daily"
    assert len(recent) == 12
    assert all(r["document"] for r in f["recent"])
    older = [r["id"] for r in f["older"]]
    assert older[0] == "report:2026-10-02:daily"
    assert older[-1] == "report:2026-09-27:daily"
    # Older reports are links to their own page, not carried on this one.
    assert all("document" not in r for r in f["older"])
    assert f["older"][0]["url"] == "/run/report:2026-10-02:daily"


def test_a_new_report_appears_on_the_next_build_with_no_edit():
    later = OBJECTS + [(f"{PREFIX}2026-10-16.md", "2026-10-16T13:30:00+00:00")]
    assert _feed(_index(objects=later))["latest"]["id"] == "report:2026-10-16:daily"


def test_the_newest_report_is_shown_even_when_it_is_older_than_the_window():
    f = _feed(_index(objects=OBJECTS[:2]))
    assert f["latest"]["id"] == "report:2026-09-28:daily"
    assert f["recent"] == [] and [r["id"] for r in f["older"]] == ["report:2026-09-27:daily"]


def test_kind_and_every_facet_must_match():
    other = Entity(kind=Kind.ARTIFACT, id="x", state="fresh",
                   provenance=Provenance("s", "2026-10-16T00:00:00Z"),
                   facets={"report": "daily"})
    index = _index()
    index.add_result(AdapterResult(name="other", status=AdapterStatus.OK,
                                   claim_class=ClaimClass.OBSERVATION, entities=(other,)))
    assert _feed(index)["latest"]["id"] == "report:2026-10-15:daily"


# --------------------------------------------------------------- the page --

def test_page_renders_latest_open_recent_collapsed_and_archive_links():
    from console.server.app import _html

    html = _html(_index(), resolve("/reports"))
    assert html.startswith("<!doctype html>")
    assert reports.REPORTS_QUESTION in html
    latest = html.split('<article class="report report-latest">', 1)[1].split("</article>", 1)[0]
    assert "<h1>Report 2026-10-15</h1>" in latest and "<strong>green</strong>" in latest
    assert html.count('<details class="report">') == 12
    assert html.index("report-latest") < html.index('<details class="report">')
    assert 'href="/run/report:2026-09-27:daily"' in html
    assert "s3://b/ops/daily/YYYY-MM-DD.md" in html and "Nothing is deleted" in html
    assert 'href="/reports">reports</a>' in html  # site nav


def test_report_text_cannot_carry_markup_or_unsafe_links():
    from console.server.app import _html

    html = _html(_index(), resolve("/reports"))
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert 'href="javascript:' not in html
    assert '<a href="https://example.com/2026-10-15">good</a>' in html


def test_an_unreadable_report_says_so_with_a_link_to_its_row():
    from console.server.app import _html

    def flaky(bucket, key):
        if key.endswith("2026-10-15.md"):
            raise OSError("denied")
        return _text(bucket, key)

    index = _index(text_reader=flaky)
    assert _feed(index)["latest"]["document"] is None
    html = _html(index, resolve("/reports"))
    assert "text could not be read (unreadable (OSError))" in html
    assert 'href="/run/report:2026-10-15:daily">open its row</a>' in html


def test_a_selector_matching_nothing_is_named_not_blank():
    from console.server.app import _html

    feeds = {"feeds": [{"id": "daily", "title": "Daily", "facets": {"report": "nope"}}]}
    html = _html(_index(feeds=feeds), resolve("/reports"))
    assert "no row matches <code>report=nope</code>" in html


def test_no_feed_declared_renders_its_absence():
    from console.server.app import _html

    html = _html(Index(), resolve("/reports"))
    assert "no report feeds declared" in html
    assert render_json.payload(Index(), resolve("/reports"))["feed"] is None


def test_other_feeds_are_linked_and_addressable():
    from console.server.app import _html

    feeds = {"feeds": FEEDS["feeds"] + [
        {"id": "weekly", "title": "Weekly", "facets": {"report": "weekly"}}]}
    index = _index(feeds=feeds)
    assert 'href="/reports/weekly">Weekly</a>' in _html(index, resolve("/reports"))
    weekly = _html(index, resolve("/reports/weekly"))
    assert "<h1>Weekly</h1>" in weekly and 'href="/reports/daily">Daily report</a>' in weekly


# ------------------------------------------------- routes and representations --

def test_routes_resolve():
    assert resolve("/reports").view == "reports"
    req = resolve("/reports/daily")
    assert req.view == "reports" and req.feed_id == "daily"
    with pytest.raises(UnknownRoute):
        resolve("/reports/a/b")


def test_json_is_the_same_query():
    index = _index()
    doc = render_json.payload(index, resolve("/reports/daily"))
    assert doc["schema_version"] == render_json.SCHEMA_VERSION
    assert doc["view"] == "reports" and doc["question"] == reports.REPORTS_QUESTION
    assert doc["feeds"] == [{"id": "daily", "title": "Daily report", "url": "/reports/daily"}]
    assert doc["feed"]["latest"]["id"] == "report:2026-10-15:daily"
    render_json.dumps(doc)


def test_an_undeclared_feed_404s_in_both_representations():
    import threading
    import urllib.error
    import urllib.request

    from console.server.app import serve

    server = serve(_index(), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for path in ("/reports/nope", "/reports/nope.json"):
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(base + path)
            assert err.value.code == 404
        with urllib.request.urlopen(base + "/reports/daily.json") as resp:
            assert resp.headers["Content-Type"].startswith("application/json")
    finally:
        server.shutdown()
        server.server_close()


def test_the_pane_registry_carries_the_question():
    [pane] = [p for p in PANES if p.name == "reports"]
    assert pane.question == reports.REPORTS_QUESTION and pane.kind is None


# ------------------------------------------------------------- declaration --

@pytest.mark.parametrize("bad, needle", [
    ([{"title": "t", "facets": {"a": "b"}}], "`id` is required"),
    ([{"id": "Bad Id", "title": "t", "facets": {"a": "b"}}], "lowercase"),
    ([{"id": "a", "title": "t", "facets": {}}], "`facets`"),
    ([{"id": "a", "title": "t", "facets": {"a": "b"}, "kind": "nope"}], "unknown `kind`"),
    ([{"id": "a", "title": "t", "facets": {"a": "b"}, "recent_days": 0}], "recent_days"),
    ([{"id": "a", "facets": {"a": "b"}}], "`title` is required"),
    ([{"id": "a", "title": "t", "facets": {"a": "b"}}] * 2, "declared twice"),
])
def test_bad_declarations_fail_by_name(bad, needle):
    with pytest.raises(reports.ReportsConfigError, match=needle):
        reports.parse({"feeds": bad})


def test_a_non_mapping_block_fails():
    with pytest.raises(reports.ReportsConfigError):
        reports.parse(["x"])


def test_example_config_declares_feeds_that_build():
    import yaml
    from pathlib import Path

    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent
                          / "config.example.yaml").read_text())
    feeds = reports.parse(cfg["reports"])
    assert feeds and feeds[0].recent_days == 14 and feeds[0].archive
    assert feeds[0].lead and feeds[0].lead.sources


# ------------------------------------------------------------- lead block --

PLAN_ADAPTER = {
    **ADAPTER, "_name": "daily-plan",
    "key_pattern": r"^ops/daily/plan-(?P<cycle>\d{4}-\d{2}-\d{2})\.md$",
    "id_template": "report:{cycle}:plan",
    "label_template": "{day:%a %-m/%-d} plan",
    "facets": {"pane": {"value": "reports"}, "report": {"value": "plan"}},
}

LEAD_OBJECTS = OBJECTS + [(f"{PREFIX}plan-2026-10-15.md", "2026-10-15T22:00:00+00:00")]

LEAD_FEEDS = {"feeds": [{**FEEDS["feeds"][0], "lead": {
    "title": "Merged by topic", "heading": "^merged (by topic|since)",
    "sources": [{"kind": "run", "facets": {"report": "daily"}},
                {"kind": "run", "facets": {"report": "plan"}}]}}]}


def _merged_text(bucket, key):
    name = key.rsplit("/", 1)[1][:-3]
    return (f"# {name}\n\n## Needs you\n- answer one\n\n"
            f"## Merged by topic ({name})\n**Governance**\n- ops#1718 five red lines\n"
            "```\n## not a heading, inside a fence\n```\n"
            "### Data\n- data#2179 FRED rows\n\n"
            "## Plan vs done\n- C1 done\n")


def _lead_index(feeds=LEAD_FEEDS, objects=LEAD_OBJECTS, text_reader=_merged_text):
    index = Index()
    for adapter in (ADAPTER, PLAN_ADAPTER):
        index.add_result(s3_records.fetch(
            adapter, lister=lambda b, p: list(objects),
            reader=lambda b, k: pytest.fail("format: object reads no parsed body"),
            text_reader=text_reader, now=NOW))
    reports.attach(index, reports.parse(feeds))
    index.finalize()
    return index


def test_section_cuts_to_the_next_heading_of_its_level_and_skips_fences():
    text = _merged_text("b", "ops/daily/2026-10-15.md")
    got = reports.section(text, re.compile("^merged by topic", re.IGNORECASE))
    assert got.startswith("## Merged by topic (2026-10-15)")
    assert "### Data" in got and "## not a heading, inside a fence" in got
    assert "Plan vs done" not in got and "Needs you" not in got
    assert reports.section("# a\n## b\n", re.compile("zzz")) is None
    # The last section runs to the end of the text.
    assert reports.section("## x\nmore\n", re.compile("x")) == "## x\nmore"


def test_lead_block_shows_each_sources_section_newest_first():
    lead = _feed(_lead_index())["lead"]
    assert lead["title"] == "Merged by topic"
    assert [i["id"] for i in lead["items"]] == [
        "report:2026-10-15:plan", "report:2026-10-15:daily"]
    for item in lead["items"]:
        assert item["section"]["text"].startswith("## Merged by topic")
        assert "Plan vs done" not in item["section"]["text"]
        assert "document" not in item  # the lead carries the section only


def test_lead_block_renders_above_the_latest_report_with_citations_safe():
    from console.server.app import _html

    html = _html(_lead_index(), resolve("/reports"))
    block = html.split('<section class="report-lead">', 1)[1].split("</section>", 1)[0]
    assert html.index('class="report-lead"') < html.index("report-latest")
    assert "<h2>Merged by topic</h2>" in block
    assert block.count('<article class="report report-lead-item">') == 2
    assert 'from <a href="/run/report:2026-10-15:plan">Thu 10/15 plan</a>' in block
    assert "ops#1718 five red lines" in block and "Plan vs done" not in block
    # The report itself is not edited: the section still reads in place.
    latest = html.split('<article class="report report-latest">', 1)[1]
    assert "Merged by topic (2026-10-15)" in latest.split("</article>", 1)[0]


def test_lead_without_sources_reads_the_feeds_own_newest_report():
    feeds = {"feeds": [{**FEEDS["feeds"][0],
                        "lead": {"title": "Merged", "heading": "merged by topic"}}]}
    lead = _feed(_lead_index(feeds=feeds))["lead"]
    assert [i["id"] for i in lead["items"]] == ["report:2026-10-15:daily"]


def test_a_lead_source_that_cannot_supply_its_section_says_why():
    from console.server.app import _html

    feeds = {"feeds": [{**FEEDS["feeds"][0], "lead": {
        "title": "Merged by topic", "heading": "^merged by topic",
        "sources": [{"facets": {"report": "daily"}}, {"facets": {"report": "nope"}}]}}]}
    index = _lead_index(feeds=feeds, text_reader=_text)  # no such heading
    lead = _feed(index)["lead"]
    assert lead["items"][0]["section"] is None
    assert "no heading matches '^merged by topic'" in lead["items"][0]["section_unavailable"]
    html = _html(index, resolve("/reports"))
    assert "no report — no row matches <code>report=nope</code>" in html
    assert "Merged by topic: no heading matches" in html


def test_no_lead_declared_renders_no_block():
    from console.server.app import _html

    assert _feed(_index())["lead"] is None
    assert "report-lead" not in _html(_index(), resolve("/reports"))


def test_json_carries_the_lead_block():
    doc = render_json.payload(_lead_index(), resolve("/reports/daily"))
    assert doc["feed"]["lead"]["items"][0]["section"]["format"] == "markdown"
    render_json.dumps(doc)


@pytest.mark.parametrize("lead, needle", [
    ("x", "lead: must be a mapping"),
    ({"heading": "a"}, "`title` is required"),
    ({"title": "t"}, "`heading` is required"),
    ({"title": "t", "heading": "("}, "not a regex"),
    ({"title": "t", "heading": "a", "sources": "x"}, "list of selectors"),
    ({"title": "t", "heading": "a", "sources": [{"facets": {}}]}, "`facets`"),
    ({"title": "t", "heading": "a", "sources": [{"facets": {"a": "b"}, "kind": "z"}]},
     "unknown `kind`"),
])
def test_bad_lead_declarations_fail_by_name(lead, needle):
    with pytest.raises(reports.ReportsConfigError, match=needle):
        reports.parse({"feeds": [{"id": "a", "title": "t", "facets": {"a": "b"}, "lead": lead}]})
