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


def test_the_landing_page_links_every_dashboard():
    index = _index(_run("cost-ci:gha", State.HEALTHY, pane="cost"))
    page = render_html.landing_page(index)
    assert "<h2>dashboards</h2>" in page
    assert '<a href="/run?pane=cost">cost</a>' in page


def test_no_dashboards_renders_as_itself_not_a_blank_region():
    index = _index(_run("plain-run", State.HEALTHY))
    page = render_html.landing_page(index)
    assert "none declared" in page.split("<h2>dashboards</h2>", 1)[1]


def test_the_json_landing_carries_the_same_index():
    index = _index(_run("cost-ci:gha", State.HEALTHY, pane="cost"))
    doc = render_json.payload(index, resolve("/"))
    assert doc["dashboards"] == index.landing_model().dashboards


def test_a_filtered_list_shows_each_rows_declared_fields():
    ent = Entity(
        kind=Kind.RUN, id="cost-ci:gha", state=State.HEALTHY, provenance=PROV,
        facets={"pane": "cost"},
        detail={"fields": {"mtd_minutes": {"value": 504, "unit": "min",
                                           "render": "count"}}},
    )
    page = render_html.list_page(_index(ent), Kind.RUN, {"pane": "cost"})
    assert "<th>fields</th>" in page
    assert "mtd_minutes 504 min" in page


def test_the_fields_cell_keeps_declared_order_and_drops_the_question():
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
    cell = page.split("<td>sev ", 1)[1].split("</td>", 1)[0]
    assert cell.startswith("SEV2 · summary Box shut itself down · age_days 3 days")
    assert "Which incidents are open?" not in page


def test_a_list_with_no_fields_keeps_the_four_field_table():
    page = render_html.list_page(
        _index(_run("plain-run", State.HEALTHY)), Kind.RUN, {})
    assert "<th>fields</th>" not in page


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
