"""The landing view's dashboard index: every `pane` facet value the rows
carry, linked to its filtered list (console-policy.md §3.1, §3.5, §4.1).

Before this, a dashboard such as `/run?pane=cost` had no navigation entry
anywhere on the surface — reachable only by someone who already knew the URL.
"""
from __future__ import annotations

from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import html as render_html
from console.render import json as render_json
from console.server.router import resolve

PROV = Provenance(source="fixture", as_of="2026-10-01T00:00:00Z",
                  evidence="fixture://test")


def _index(*entities: Entity) -> Index:
    index = Index()
    index.add_result(AdapterResult(
        name="f", status=AdapterStatus.OK, claim_class=ClaimClass.DECLARATION,
        entities=entities,
    ))
    index.finalize()
    return index


def _run(rid: str, state: State, **facets: str) -> Entity:
    return Entity(kind=Kind.RUN, id=rid, state=state, provenance=PROV,
                  facets=facets)


def test_each_pane_value_is_listed_with_its_filtered_list_url():
    index = _index(
        _run("cost-ci:gha", State.HEALTHY, pane="cost", meter="ci"),
        _run("cost-ci:codebuild", State.DEGRADED, pane="cost", meter="ci"),
        _run("sev:2026-10", State.HEALTHY, pane="sev"),
        _run("plain-run", State.HEALTHY),
    )
    assert index.landing_model().dashboards == [
        {"pane": "cost", "kind": "run", "url": "/run?pane=cost",
         "rows": 2, "not_healthy": 1},
        {"pane": "sev", "kind": "run", "url": "/run?pane=sev",
         "rows": 1, "not_healthy": 0},
    ]


def test_a_pane_spanning_two_kinds_gets_one_link_per_kind():
    index = _index(
        _run("cost-ci:gha", State.HEALTHY, pane="cost"),
        Entity(kind=Kind.COMPONENT, id="comp-cost", state=State.HEALTHY,
               provenance=PROV, facets={"pane": "cost"}),
    )
    urls = [d["url"] for d in index.landing_model().dashboards]
    assert urls == ["/component?pane=cost", "/run?pane=cost"]


def test_the_dashboards_page_links_every_dashboard():
    index = _index(_run("cost-ci:gha", State.HEALTHY, pane="cost"))
    page = render_html.dashboards_page(index)
    assert '<a href="/run?pane=cost">cost</a>' in page
    assert "which dashboards and lists exist" in page  # §4.4 question rendered


def test_no_dashboards_renders_as_itself_not_a_blank_region():
    index = _index(_run("plain-run", State.HEALTHY))
    page = render_html.dashboards_page(index)
    assert "none declared" in page.split("<h2>dashboards</h2>", 1)[1]


def test_the_json_dashboards_view_carries_the_same_index():
    index = _index(_run("cost-ci:gha", State.HEALTHY, pane="cost"))
    doc = render_json.payload(index, resolve("/dashboards"))
    assert doc["view"] == "dashboards"
    assert doc["dashboards"] == index.landing_model().dashboards
    assert {"kind": "run", "url": "/run", "rows": 1} in doc["kinds"]


def test_the_landing_view_stays_the_exception_list():
    """§4.3: the landing view is exceptions-first; the index is its own pane
    (§4.4 — one question, one place), reached from the site menu."""
    index = _index(_run("cost-ci:gha", State.HEALTHY, pane="cost"))
    assert "<h2>dashboards</h2>" not in render_html.landing_page(index)


def test_every_page_carries_the_site_menu():
    from console.server.app import _html
    index = _index(_run("cost-ci:gha", State.HEALTHY, pane="cost"))
    for route in ("/", "/dashboards", "/run?pane=cost", "/run/cost-ci:gha",
                  "/search?q=cost"):
        path, _, query = route.partition("?")
        page = _html(index, resolve(path, query))
        assert '<a href="/dashboards">dashboards</a>' in page, route


def test_a_filtered_list_shows_each_rows_declared_fields():
    ent = Entity(
        kind=Kind.RUN, id="cost-ci:gha", state=State.HEALTHY, provenance=PROV,
        facets={"pane": "cost"},
        detail={"fields": {"mtd_minutes": {"value": 504, "unit": "min",
                                           "render": "count"}}},
    )
    page = render_html.list_page(_index(ent), Kind.RUN, {"pane": "cost"})
    assert "<th>mtd_minutes</th>" in page
    assert "<td>504 min</td>" in page


def test_declared_fields_are_columns_in_declared_order_without_the_question():
    ent = Entity(
        kind=Kind.INCIDENT, id="ops-I1", state=State.HEALTHY, provenance=PROV,
        facets={"pane": "incidents"},
        detail={"fields": {
            "question": {"value": "Which incidents are open?", "render": "text"},
            "sev": {"value": "SEV2", "render": "value"},
            "summary": {"value": "Box shut itself down", "render": "text"},
            "age_days": {"value": 3, "unit": "days", "render": "count"},
        }},
    )
    page = render_html.list_page(_index(ent), Kind.INCIDENT, {"pane": "incidents"})
    head = page.split("<thead>", 1)[1].split("</thead>", 1)[0]
    assert head.index("<th>state</th>") < head.index("<th>sev</th>") \
        < head.index("<th>summary</th>") < head.index("<th>age_days</th>") < head.index("<th>source</th>")
    assert "<td>SEV2</td><td>Box shut itself down</td><td>3 days</td>" in page
    assert "Which incidents are open?" not in page


def test_a_row_missing_a_declared_field_renders_it_absent():
    a = Entity(kind=Kind.RUN, id="a", state=State.HEALTHY, provenance=PROV, facets={"pane": "p"},
               detail={"fields": {"x": {"value": 1, "render": "count"}}})
    b = Entity(kind=Kind.RUN, id="b", state=State.HEALTHY, provenance=PROV, facets={"pane": "p"},
               detail={"fields": {"y": {"value": 2, "render": "count"}}})
    page = render_html.list_page(_index(a, b), Kind.RUN, {"pane": "p"})
    assert "<th>x</th><th>y</th>" in page
    assert page.count('<em class="absent">—</em>') == 2


def test_a_list_with_no_fields_keeps_the_four_field_table():
    page = render_html.list_page(
        _index(_run("plain-run", State.HEALTHY)), Kind.RUN, {})
    assert page.count("<th>") == 5  # id, state, source, as-of, evidence


def test_every_dashboard_link_filters_when_resolved_through_the_router():
    """The link must survive the real request path, not only `list_page`
    called directly: the router admits only declared facets, and a dropped
    `pane` renders the whole unfiltered kind under the dashboard's link."""
    index = _index(
        _run("cost-ci:gha", State.HEALTHY, pane="cost"),
        _run("ae-preflight-sweep", State.FAILED),
    )
    for d in index.landing_model().dashboards:
        path, _, query = d["url"].partition("?")
        req = resolve(path, query)
        page = render_html.list_page(index, req.kind, req.facets)
        assert "cost-ci:gha" in page
        assert "ae-preflight-sweep" not in page


def test_the_landing_view_links_every_dashboard_in_one_line():
    from console.render import html as render_html
    out = render_html.dashboard_links([
        {"pane": "cost", "kind": "run", "url": "/run?pane=cost", "rows": 3, "not_healthy": 2},
        {"pane": "incidents", "kind": "incident", "url": "/incident?pane=incidents", "rows": 5, "not_healthy": 0},
    ])
    assert 'href="/run?pane=cost">cost</a>' in out and "(2 not healthy)" in out
    assert 'href="/incident?pane=incidents">incidents</a>' in out
    assert 'href="/dashboards">all</a>' in out
    assert render_html.dashboard_links([]) == ""
