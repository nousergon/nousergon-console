"""Dated list views and documents shown in place (the Director's reports pane).

A list whose rows declare `window_days` opens on that window, newest first,
and links to an archive of every row (`?days=all`) that links back. A row
carrying `detail["document"]` shows that text, escaped, on its entity page,
so a report is read where it is listed instead of behind an `s3://` link a
browser cannot open.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import html as render_html
from console.render import json as render_json
from console.server.router import resolve

NOW = datetime.now(timezone.utc)


def _report(rid: str, age_days: float | None, window: int | None = 7,
            document: dict | None = None, **facets: str) -> Entity:
    as_of = None if age_days is None else (NOW - timedelta(days=age_days)).isoformat()
    detail: dict = {}
    if window is not None:
        detail["window_days"] = window
    if document is not None:
        detail["document"] = document
    return Entity(kind=Kind.RUN, id=rid, state=State.HEALTHY,
                  provenance=Provenance(source="s3://b/reports/", as_of=as_of,
                                        evidence=f"s3://b/reports/{rid}.md"),
                  facets={"pane": "reports", **facets}, detail=detail)


def _index(*entities: Entity) -> Index:
    index = Index()
    index.add_result(AdapterResult(
        name="f", status=AdapterStatus.OK, claim_class=ClaimClass.OBSERVATION,
        entities=entities,
    ))
    index.finalize()
    return index


def _week_and_older() -> Index:
    return _index(
        _report("report:d-2:evening-plan", 2),
        _report("report:d-1:nightly-run", 1),
        _report("report:d-0:morning-report", 0.1),
        _report("report:d-30:evening-plan", 30),
        _report("report:undated", None),
    )


# ------------------------------------------------------------------ router --

def test_days_param_resolves_a_window_or_the_archive():
    assert resolve("/run", "pane=reports&days=3").days == 3
    archive = resolve("/run", "pane=reports&days=all")
    assert archive.days is None and archive.days_all is True
    assert archive.facets == {"pane": "reports"}
    for junk in ("days=0", "days=-2", "days=week", ""):
        req = resolve("/run", junk)
        assert req.days is None and req.days_all is False


# ------------------------------------------------------------- list_rows --

def test_a_declared_window_opens_on_its_last_days_newest_first():
    listing = render_json.list_rows(_week_and_older(), resolve("/run", "pane=reports"))
    assert [e.id for e in listing.rows] == [
        "report:d-0:morning-report", "report:d-1:nightly-run", "report:d-2:evening-plan"]
    assert listing.dated and not listing.collapsed
    assert listing.window_days == 7 and listing.declared_window == 7
    assert listing.outside == 2  # one older, one undated


def test_the_archive_is_every_row_newest_first_undated_last():
    listing = render_json.list_rows(_week_and_older(),
                                    resolve("/run", "pane=reports&days=all"))
    assert [e.id for e in listing.rows] == [
        "report:d-0:morning-report", "report:d-1:nightly-run",
        "report:d-2:evening-plan", "report:d-30:evening-plan", "report:undated"]
    assert listing.window_days is None and listing.outside == 0


def test_an_explicit_days_overrides_the_declared_window():
    listing = render_json.list_rows(_week_and_older(),
                                    resolve("/run", "pane=reports&days=1"))
    assert [e.id for e in listing.rows] == ["report:d-0:morning-report"]


def test_a_mixed_pane_is_not_windowed_by_one_of_its_sources():
    """Only when EVERY row declares the same window: otherwise the other
    source's older rows would vanish from its own dashboard unannounced."""
    index = _index(_report("report:old", 30),
                   _report("other:old", 30, window=None))
    listing = render_json.list_rows(index, resolve("/run", "pane=reports"))
    assert not listing.dated
    assert {e.id for e in listing.rows} == {"report:old", "other:old"}


def test_an_unwindowed_run_list_still_collapses_per_job():
    index = _index(_report("job@2026-10-01", 2, window=None),
                   _report("job@2026-10-02", 1, window=None))
    listing = render_json.list_rows(index, resolve("/run", ""))
    assert listing.collapsed and [e.id for e in listing.rows] == ["job@2026-10-02"]


# ------------------------------------------------------------------- HTML --

def test_the_window_page_links_the_archive_and_the_archive_links_back():
    index = _week_and_older()
    page = render_html.list_page(index, Kind.RUN, {"pane": "reports"})
    assert "past 7 days, newest first" in page
    assert 'href="/run?days=all&amp;pane=reports">archive: every run (5)</a>' in page
    assert "2 older or undated not shown" in page
    assert "report:d-30:evening-plan" not in page
    assert "show every run" not in page  # the per-job toggle does not apply

    archive = render_html.list_page(index, Kind.RUN, {"pane": "reports"}, days_all=True)
    assert "report:d-30:evening-plan" in archive
    assert 'href="/run?pane=reports">past 7 days</a>' in archive


def test_a_long_archive_pages_by_links_that_keep_the_view():
    index = _index(*(_report(f"report:{i:03d}", i / 10) for i in range(120)))
    first = render_html.list_page(index, Kind.RUN, {"pane": "reports"}, days_all=True)
    assert "page 1 of 3" in first
    assert 'href="/run?days=all&amp;page=2&amp;pane=reports">next page</a>' in first
    second = render_html.list_page(index, Kind.RUN, {"pane": "reports"}, page=2,
                                   days_all=True)
    assert "previous page" in second and "next page" in second
    req = resolve("/run", "days=all&page=2&pane=reports")
    assert (req.page, req.days_all, req.facets) == (2, True, {"pane": "reports"})


def test_a_one_page_list_has_no_pager():
    page = render_html.list_page(_week_and_older(), Kind.RUN, {"pane": "reports"})
    assert 'class="pager"' not in page


def test_the_entity_page_shows_the_document_escaped():
    ent = _report("report:d-0:morning-report", 0.1, document={
        "text": "# Morning report\n\n<script>alert(1)</script> & done",
        "bytes": 52, "truncated": False})
    page = render_html.entity_page(_index(ent), ent)
    assert '<pre class="document"># Morning report' in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; done" in page
    assert "<script>" not in page
    assert "document cut" not in page


def test_a_cut_document_says_so():
    ent = _report("r", 0, document={"text": "head", "bytes": 900000, "truncated": True})
    page = render_html.entity_page(_index(ent), ent)
    assert "document cut" in page and "900000 bytes" in page


def test_no_document_no_section():
    ent = _report("r", 0)
    assert "<h2>document</h2>" not in render_html.entity_page(_index(ent), ent)


# ------------------------------------------------------------------- JSON --

def test_the_json_list_carries_the_window_and_not_the_document_text():
    doc = {"text": "long body", "bytes": 9, "truncated": False}
    index = _index(_report("r:new", 1, document=doc), _report("r:old", 30))
    body = render_json.payload(index, resolve("/run", "pane=reports"))
    assert body["window"] == {"days": 7, "declared_days": 7, "outside": 1}
    (row,) = body["entities"]
    assert row["detail"]["document"] == {"bytes": 9, "truncated": False}
    entity = render_json.payload(index, resolve("/run/r:new", ""))
    assert entity["entity"]["detail"]["document"]["text"] == "long body"
