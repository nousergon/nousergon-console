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


# The next slice of I11805: the landing view's exception list reads the same
# way a run list does — runs named by their job, one row per job per state.

FAILING = [
    _run("job-a@2026-10-01T01:00:00Z", State.FAILED, "2026-10-01T01:00:00Z"),
    _run("job-a@2026-10-01T02:00:00Z", State.FAILED, "2026-10-01T02:00:00Z"),
    _run("job-a@2026-10-01T03:00:00Z", State.DEGRADED, "2026-10-01T03:00:00Z"),
    _run("job-b@2026-10-01T01:30:00Z", State.FAILED, "2026-10-01T01:30:00Z"),
]


def test_the_landing_names_a_run_by_its_job_and_folds_its_runs_per_state():
    page = _page(_index(FAILING), "/")
    failed = page.split('<details id="not-healthy-FAILED" open>')[1].split("</details>")[0]
    # job-a's two FAILED runs read as one row, named for the job, newest run linked.
    assert ">job-a (2 runs)</a>" in failed
    assert "/run/job-a@2026-10-01T02:00:00Z" in failed
    assert "job-a@2026-10-01T01:00:00Z" not in failed
    assert ">job-b</a>" in failed
    # A different state is a different fact: job-a DEGRADED still has its own row.
    degraded = page.split('<details id="not-healthy-DEGRADED">')[1].split("</details>")[0]
    assert ">job-a</a>" in degraded
    assert "3 rows (newest run per job, from 4 rows)" in page
    assert 'href="#not-healthy-FAILED">2 FAILED</a>' in page


def test_the_landing_fold_never_changes_the_not_healthy_count():
    page = _page(_index(FAILING), "/")
    assert "· 4 not healthy ·" in page
    doc = render_json.payload(_index(FAILING), resolve("/"))
    assert len(doc["exceptions"]) == 4


def test_landing_json_carries_the_same_fold_and_names():
    doc = render_json.payload(_index(FAILING), resolve("/"))
    view = doc["exception_view"]
    assert set(view["latest_per_job"]) == {
        "job-a@2026-10-01T02:00:00Z", "job-a@2026-10-01T03:00:00Z",
        "job-b@2026-10-01T01:30:00Z"}
    assert view["labels"]["job-a@2026-10-01T02:00:00Z"] == "job-a"
    assert view["runs"] == {"job-a@2026-10-01T02:00:00Z": 2,
                            "job-a@2026-10-01T03:00:00Z": 1,
                            "job-b@2026-10-01T01:30:00Z": 1}


def test_a_landing_row_that_is_not_a_run_keeps_its_declared_label():
    comp = Entity(kind=Kind.COMPONENT, id="ae-x.service", state=State.FAILED,
                  provenance=Provenance(source="fixture", as_of="2026-10-01T01:00:00Z"),
                  detail={"label": "Nightly export"})
    page = _page(_index([comp]), "/")
    assert ">Nightly export</a>" in page and "/component/ae-x.service" in page
    assert "newest run per job" not in page
    view = render_json.payload(_index([comp]), resolve("/"))["exception_view"]
    assert view == {"latest_per_job": ["ae-x.service"],
                    "labels": {"ae-x.service": "Nightly export"}, "runs": {}}


# The next slice of I11805: an entity page's relations read as a list — one
# table per predicate, each row with its state and its job's name, runs folded
# to the newest per job and state, a count by state, not healthy first.

COMPONENT = Entity(kind=Kind.COMPONENT, id="job-a", state=State.FAILED,
                   provenance=Provenance(source="fixture", as_of="2026-10-01T03:00:00Z"))
JOB_A_RUNS = [
    _run("job-a@2026-10-01T01:00:00Z", State.HEALTHY, "2026-10-01T01:00:00Z"),
    _run("job-a@2026-10-01T02:00:00Z", State.HEALTHY, "2026-10-01T02:00:00Z"),
    _run("job-a@2026-10-01T03:00:00Z", State.FAILED, "2026-10-01T03:00:00Z"),
]
BELONGS = [Edge(source=r.id, rel="belongs-to", target="job-a") for r in JOB_A_RUNS]


def _component_index():
    edges = BELONGS + [Edge(source="job-a", rel="produces", target="s3://b/missing.json")]
    return _index([COMPONENT, *JOB_A_RUNS], edges)


def test_an_entity_page_folds_its_runs_and_counts_them_by_state():
    page = _page(_component_index(), "/component/job-a")
    rel = page.split("<h2>relations</h2>")[1]
    contains = rel.split('<h3 class="list-group">contains</h3>')[1].split("<h3")[0]
    assert "2 rows (from 3 related)" in contains
    assert '1 FAILED</span>' in contains and '1 HEALTHY</span>' in contains
    # Not healthy first; the folded HEALTHY row is the newest one, named for its job.
    assert contains.index("2026-10-01T03:00:00Z") < contains.index("2026-10-01T02:00:00Z")
    assert ">job-a (2 runs)</a>" in contains
    assert "/run/job-a@2026-10-01T02:00:00Z" in contains
    assert "/run/job-a@2026-10-01T01:00:00Z" not in contains
    assert 'href="/component/job-a?runs=all">show every run</a>' in rel


def test_an_entity_page_lists_every_run_when_the_url_says_so():
    page = _page(_component_index(), "/component/job-a?runs=all")
    rel = page.split("<h2>relations</h2>")[1]
    assert "/run/job-a@2026-10-01T01:00:00Z" in rel
    assert "3 rows ·" in rel and "from 3 related" not in rel
    assert 'href="/component/job-a">show newest run per job and state</a>' in rel


def test_a_relation_to_nothing_in_the_index_is_still_shown():
    page = _page(_component_index(), "/component/job-a")
    produces = page.split('<h3 class="list-group">produces</h3>')[1]
    assert "0 rows ·" in produces and "1 not in the index:" in produces
    assert "<li>s3://b/missing.json</li>" in produces


def test_a_run_page_names_its_job_and_shows_no_fold_toggle():
    page = _page(_component_index(), "/run/job-a@2026-10-01T01:00:00Z")
    rel = page.split("<h2>relations</h2>")[1]
    assert '<h3 class="list-group">belongs-to</h3>' in rel
    assert '1 FAILED</span>' in rel and "show every run" not in rel


def test_entity_json_carries_the_same_relation_view():
    index = _component_index()
    doc = render_json.payload(index, resolve("/component/job-a"))
    groups = {g["rel"]: g for g in doc["relation_view"]["groups"]}
    assert doc["relation_view"]["all_runs"] is False
    assert groups["contains"]["rows"] == ["job-a@2026-10-01T03:00:00Z",
                                          "job-a@2026-10-01T02:00:00Z"]
    assert groups["contains"]["related"] == 3
    assert groups["contains"]["summary"] == {"FAILED": 1, "HEALTHY": 1}
    assert groups["contains"]["labels"]["job-a@2026-10-01T02:00:00Z"] == "job-a"
    assert groups["contains"]["runs"]["job-a@2026-10-01T02:00:00Z"] == 2
    assert groups["produces"]["unresolved"] == ["s3://b/missing.json"]
    # The raw edges are unchanged: the projection is added beside them.
    assert len(doc["relations"]) == 4
    every = render_json.payload(index, resolve("/component/job-a", "runs=all"))
    assert every["relation_view"]["all_runs"] is True
    assert len({g["rel"]: g for g in every["relation_view"]["groups"]}["contains"]["rows"]) == 3


def test_an_entity_with_no_relations_says_so():
    page = _page(_index([COMPONENT]), "/component/job-a")
    assert '<h2>relations</h2><p class="absent">no relations</p>' in page
    assert render_json.payload(_index([COMPONENT]), resolve("/component/job-a"))[
        "relation_view"]["groups"] == []


# ------------------------------------------------------------------ search --

def _search_index() -> Index:
    return _index(RUNS + [
        _run("job-alpha@2026-10-01T04:00:00Z", State.HEALTHY, "2026-10-01T04:00:00Z"),
        Entity(kind=Kind.COMPONENT, id="job-a", state=State.HEALTHY,
               provenance=Provenance(source="fixture", as_of="2026-10-01T00:00:00Z"),
               detail={"label": "Job A pipeline"}),
    ])


def test_search_names_runs_by_job_and_folds_them_per_state():
    page = _page(_search_index(), "/search?q=job-a")
    assert ">Job A pipeline</a>" in page  # the exact hit, by its declared label
    runs = page.split('<h2 class="list-group">runs</h2>')[1]
    # job-a's two runs differ in state, so each is its own row, named by job.
    assert ">job-a</a>" in runs and "job-a@2026-10-01T02:00:00Z" in runs
    assert ">job-alpha</a>" in runs
    assert "3 rows · " in runs
    assert "1 FAILED</span>" in runs and "2 HEALTHY</span>" in runs
    # Not healthy first: the FAILED run leads its group.
    assert runs.index("state-FAILED") < runs.index('class="state-HEALTHY"')


def test_search_declares_the_fold_and_links_every_run():
    index = _index([
        _run("job-a@2026-10-01T01:00:00Z", State.HEALTHY, "2026-10-01T01:00:00Z"),
        _run("job-a@2026-10-01T02:00:00Z", State.HEALTHY, "2026-10-01T02:00:00Z"),
    ])
    page = _page(index, "/search?q=job-a")
    assert ">job-a (2 runs)</a>" in page
    assert "1 row (from 2 matches)" in page
    assert "job-a@2026-10-01T01:00:00Z" not in page  # the older run is folded
    assert 'href="/search?q=job-a&amp;runs=all">show every run</a>' in page
    every = _page(index, "/search?q=job-a&runs=all")
    assert "job-a@2026-10-01T01:00:00Z" in every
    assert "show newest run per job and state" in every


def test_an_exact_run_id_is_never_folded_away():
    index = _index([
        _run("job-a@2026-10-01T01:00:00Z", State.FAILED, "2026-10-01T01:00:00Z"),
        _run("job-a@2026-10-01T02:00:00Z", State.FAILED, "2026-10-01T02:00:00Z"),
    ])
    page = _page(index, "/search?q=job-a@2026-10-01T01:00:00Z")
    exact = page.split('<h2 class="list-group">exact match</h2>')[1]
    assert 'href="/run/job-a@2026-10-01T01:00:00Z"' in exact


def test_search_with_no_query_asks_for_one_rather_than_reporting_no_match():
    page = _page(_search_index(), "/search")
    assert "Type an identifier" in page and "no matches" not in page
    assert "no matches — no identifier or name in the fleet contains this" in _page(
        _search_index(), "/search?q=nothing-here")


def test_search_json_carries_the_same_view():
    index = _search_index()
    doc = render_json.payload(index, resolve("/search", "q=job-a"))
    view = doc["search_view"]
    assert view["all_runs"] is False
    assert view["exact"] == ["job-a"] and view["exact_labels"] == {"job-a": "Job A pipeline"}
    runs = {g["kind"]: g for g in view["groups"]}["run"]
    assert runs["matched"] == 3
    assert runs["rows"][0] == "job-a@2026-10-01T01:00:00Z"  # FAILED first
    assert runs["summary"] == {"FAILED": 1, "HEALTHY": 2}
    assert runs["labels"]["job-alpha@2026-10-01T04:00:00Z"] == "job-alpha"
    # The raw hits are unchanged: the projection is added beside them.
    assert len(doc["hits"]) == 4 and doc["hits"][0]["exact"]
    every = render_json.payload(index, resolve("/search", "q=job-a&runs=all"))
    assert every["search_view"]["all_runs"] is True


def test_search_finds_a_row_by_the_name_its_page_shows():
    # A reader types what a page showed them, not the id behind it.
    page = _page(_search_index(), "/search?q=job+a+pipeline")
    assert ">Job A pipeline</a>" in page and 'href="/component/job-a"' in page
    assert "1 row · 1 by name · " in page
    assert "no matches" not in page


def test_search_json_says_which_hits_a_name_found():
    doc = render_json.payload(_search_index(), resolve("/search", "q=pipeline"))
    assert [(h["entity"]["id"], h["matched_on"]) for h in doc["hits"]] == [
        ("job-a", "label")]
    assert doc["search_view"]["by_label"] == 1
    assert doc["search_view"]["groups"][0]["labels"] == {"job-a": "Job A pipeline"}
    by_id = render_json.payload(_search_index(), resolve("/search", "q=job-a"))
    assert by_id["search_view"]["by_label"] == 0
    assert {h["matched_on"] for h in by_id["hits"]} == {"id"}


# --------------------------------------- history and doctor pages (I11805) ---

def _history_entity(label=None):
    from datetime import datetime, timezone  # noqa: F401
    from console.model.entity import Entity, Provenance
    from console.model.kinds import Kind, State

    detail = {
        "history_retention_hours": 48,
        "history": [
            {"as_of": "2026-08-04T10:00:00Z", "value": 3, "state": "HEALTHY"},
            {"as_of": "2026-08-04T11:00:00Z", "value": 4},
        ],
    }
    if label:
        detail["label"] = label
    return Entity(kind=Kind.COMPONENT, id="ae-writer.service", state=State.FAILED,
                  provenance=Provenance(source="fixture"), detail=detail)


def _history_html(ent):
    from datetime import datetime, timezone
    from console.render.html import history_page

    return history_page(Index(), ent, 24, now=datetime(2026, 8, 4, 12, tzinfo=timezone.utc))


def test_history_page_is_a_table_with_a_breadcrumb_and_window_links():
    html = _history_html(_history_entity(label="Nightly writer"))
    assert "{'as_of'" not in html  # no dict repr
    assert "<h1>history: Nightly writer</h1>" in html
    assert 'href="/component/ae-writer.service"' in html
    assert 'href="/component"' in html
    assert "<th>as_of</th><th>value</th><th>state</th>" in html
    assert "<td>2026-08-04T11:00:00Z</td><td>4</td>" in html
    assert "2 observations" in html and "state now: FAILED" in html
    # The current window is plain text; the others are links that carry the URL.
    assert "<strong>24h</strong>" in html
    assert 'href="/history/component/ae-writer.service?window_hours=168"' in html


def test_history_page_without_retention_says_so_and_still_links_back():
    from console.model.entity import Entity, Provenance
    from console.model.kinds import Kind, State
    from console.render.html import history_page

    ent = Entity(kind=Kind.COMPONENT, id="x", state=State.HEALTHY,
                 provenance=Provenance(source="fixture"))
    html = history_page(Index(), ent, 24)
    assert "history unavailable" in html and 'href="/component/x"' in html


def test_entity_page_links_history_only_where_retained_and_always_doctor():
    from console.render.html import _entity_links

    assert 'href="/history/component/ae-writer.service"' in _entity_links(_history_entity())
    assert 'href="/doctor/ae-writer.service"' in _entity_links(_history_entity())
    from console.model.entity import Entity, Provenance
    from console.model.kinds import Kind, State
    bare = Entity(kind=Kind.COMPONENT, id="x", state=State.HEALTHY,
                  provenance=Provenance(source="fixture"))
    assert "/history/" not in _entity_links(bare)
    assert 'href="/doctor/x"' in _entity_links(bare)
