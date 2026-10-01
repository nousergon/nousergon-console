"""alpha-engine-config-I11805: list pages that read at a glance.

A run list shows the newest run per job by default (`?runs=all` shows every
run), every list opens with a count by state, and rows that are not healthy
sort first. HTML and JSON read the same `list_rows` (§3.8).
"""
from __future__ import annotations

from console.index.graph import Index
from console.model.entity import Edge, Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import html as render_html
from console.render import json as render_json
from console.server.app import _html
from console.server.router import resolve


def _run(rid: str, state: State, as_of: str) -> Entity:
    return Entity(kind=Kind.RUN, id=rid, state=state,
                  provenance=Provenance(source="fixture", as_of=as_of))


def _index(entities, edges=()) -> Index:
    index = Index()
    index.add_result(AdapterResult(
        name="f", status=AdapterStatus.OK, claim_class=ClaimClass.DECLARATION,
        entities=tuple(entities), edges=tuple(edges),
    ))
    index.finalize()
    return index


RUNS = [
    _run("job-a@2026-10-01T01:00:00Z", State.FAILED, "2026-10-01T01:00:00Z"),
    _run("job-a@2026-10-01T02:00:00Z", State.HEALTHY, "2026-10-01T02:00:00Z"),
    _run("job-b@2026-10-01T01:30:00Z", State.DEGRADED, "2026-10-01T01:30:00Z"),
    _run("cost-ci:gha", State.HEALTHY, "2026-10-01T03:00:00Z"),
]


def _page(index, route):
    path, _, query = route.partition("?")
    return _html(index, resolve(path, query))


def test_a_run_list_shows_the_newest_run_per_job_by_default():
    page = _page(_index(RUNS), "/run")
    assert "job-a@2026-10-01T02:00:00Z" in page
    assert "job-a@2026-10-01T01:00:00Z" not in page  # superseded run hidden
    assert "3 jobs from 4 runs" in page
    assert ">job-a</a>" in page  # labelled by job, not job@timestamp


def test_the_every_run_link_round_trips_and_shows_history():
    page = _page(_index(RUNS), "/run?runs=all")
    assert "job-a@2026-10-01T01:00:00Z" in page
    assert "show newest run per job" in page
    assert 'href="/run?runs=all"' in _page(_index(RUNS), "/run")


def test_a_belongs_to_edge_names_the_job():
    runs = [_run("r1", State.HEALTHY, "2026-10-01T01:00:00Z"),
            _run("r2", State.FAILED, "2026-10-01T02:00:00Z")]
    edges = [Edge(source="r1", rel="belongs-to", target="comp-x"),
             Edge(source="r2", rel="belongs-to", target="comp-x")]
    listing = render_json.list_rows(_index(runs, edges), resolve("/run"))
    assert [e.id for e in listing.rows] == ["r2"]
    assert listing.labels == {"r2": "comp-x"}


def test_the_summary_counts_states_and_not_healthy_sorts_first():
    listing = render_json.list_rows(_index(RUNS), resolve("/run"))
    assert listing.summary == {"DEGRADED": 1, "HEALTHY": 2}
    assert listing.rows[0].state is State.DEGRADED
    page = _page(_index(RUNS), "/run")
    assert "3 jobs" in page and "1 DEGRADED" in page and "2 HEALTHY" in page


def test_json_carries_the_same_projection():
    index = _index(RUNS)
    doc = render_json.payload(index, resolve("/run"))
    assert doc["latest_per_job"] == {"jobs": 3, "runs": 4}
    assert doc["summary"] == {"DEGRADED": 1, "HEALTHY": 2}
    assert [e["id"] for e in doc["entities"]][0] == "job-b@2026-10-01T01:30:00Z"
    full = render_json.payload(index, resolve("/run", "runs=all"))
    assert full["filtered"] == 4 and "latest_per_job" not in full


def test_non_run_lists_are_not_collapsed():
    comps = [Entity(kind=Kind.COMPONENT, id=f"c@{i}", state=State.HEALTHY,
                    provenance=Provenance(source="fixture")) for i in range(2)]
    page = render_html.list_page(_index(comps), Kind.COMPONENT, {})
    assert "2 components" in page and "newest run per job" not in page


def _fielded(rid: str, source: str, question: str, fields: dict) -> Entity:
    return Entity(
        kind=Kind.RUN, id=rid, state=State.HEALTHY,
        provenance=Provenance(source=source, as_of="2026-10-01T03:00:00Z"),
        detail={"fields": {"question": {"value": question, "render": "text"},
                           **fields}},
    )


def test_a_list_from_several_sources_renders_a_table_per_source():
    spend = _fielded("cost-spend:aws", "s3://b/spend", "Is spend on track?",
                     {"mtd_usd": {"value": 3.0, "unit": "usd", "baseline": None}})
    ci = _fielded("cost-ci:gha", "s3://b/ci", "Are CI minutes on track?",
                  {"mtd_minutes": {"value": 9, "unit": "minutes", "baseline": None}})
    page = _page(_index([spend, ci]), "/run")
    assert page.count("<table>") == 2
    assert "<h2 class=\"list-group\">Is spend on track?</h2>" in page
    assert "<h2 class=\"list-group\">Are CI minutes on track?</h2>" in page
    spend_table = page.split("Is spend on track?</h2>")[1].split("</table>")[0]
    assert "mtd_usd" in spend_table and "mtd_minutes" not in spend_table


def test_a_list_from_one_source_stays_one_table_without_a_heading():
    a = _fielded("cost-spend:aws", "s3://b/spend", "q", {"x": {"value": "1", "render": "text"}})
    b = _fielded("cost-spend:neon", "s3://b/spend", "q", {"x": {"value": "2", "render": "text"}})
    page = _page(_index([a, b]), "/run")
    assert page.count("<table>") == 1 and "list-group" not in page


def test_the_landing_view_groups_exceptions_by_state_worst_first():
    rows = [
        _run("old-probe", State.UNREPORTED, "2026-08-01T00:00:00Z"),
        _run("fresh-fail", State.FAILED, "2026-10-01T05:00:00Z"),
        _run("older-fail", State.FAILED, "2026-09-01T05:00:00Z"),
        _run("ok", State.HEALTHY, "2026-10-01T05:00:00Z"),
    ]
    page = _page(_index(rows), "/")
    assert "3 rows" in page and 'href="#not-healthy-FAILED">2 FAILED</a>' in page
    assert '<details id="not-healthy-FAILED" open>' in page
    assert '<details id="not-healthy-UNREPORTED">' in page  # collapsed, still present
    assert page.index("fresh-fail") < page.index("older-fail") < page.index("old-probe")
