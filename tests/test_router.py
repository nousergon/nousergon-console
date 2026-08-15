"""Router tests — the §3.2 round-trip: a URL resolves to the same view, cold."""
from __future__ import annotations

import pytest

from console.server.router import (
    Resolved, UnknownRoute, path_for_entity, path_for_list, resolve,
)
from console.model.kinds import Kind


def test_entity_route_resolves():
    r = resolve("/component/comp-alpha")
    assert r.view == "entity"
    assert r.kind is Kind.COMPONENT
    assert r.entity_id == "comp-alpha"


def test_entity_route_round_trips_cold():
    # A pasted URL reproduces the view with no prior client state: resolving
    # the canonical path for an entity yields that same entity's resolution.
    path = path_for_entity(Kind.ARTIFACT, "s3://b/k.json")
    r = resolve(path)
    assert (r.view, r.kind, r.entity_id) == ("entity", Kind.ARTIFACT, "s3://b/k.json")


def test_list_route_with_facets_in_url():
    r = resolve("/component", "substrate=lambda&owner=brian")
    assert r.view == "list"
    assert r.kind is Kind.COMPONENT
    assert r.facets == {"substrate": "lambda", "owner": "brian"}


def test_list_route_ignores_non_facet_params():
    r = resolve("/component", "substrate=lambda&bogus=x")
    assert r.facets == {"substrate": "lambda"}


def test_list_route_keeps_page_in_the_url():
    r = resolve("/component", "owner=brian&page=3")
    assert r.page == 3


def test_list_path_round_trips_with_sorted_facets():
    path = path_for_list(Kind.COMPONENT, {"owner": "brian", "substrate": "lambda"})
    r = resolve(path.split("?")[0], path.split("?")[1])
    assert r.facets == {"owner": "brian", "substrate": "lambda"}


def test_landing_is_root():
    assert resolve("/").view == "landing"
    assert resolve("").view == "landing"


def test_search_route():
    r = resolve("/search", "q=comp-alpha")
    assert r.view == "search"
    assert r.query == "comp-alpha"


def test_kind_segments_are_literal_kind_names():
    for kind in Kind:
        r = resolve(f"/{kind.route}")
        assert r.kind is kind


def test_unknown_segment_raises_not_blank():
    with pytest.raises(UnknownRoute):
        resolve("/widgets/abc")


def test_entity_id_may_contain_slashes():
    # An artifact's id is its object key — slashes and all (§3.2: the
    # identifier is the URL). The remainder after /<kind>/ is the id.
    r = resolve("/artifact/ops/checks/comp-alpha/latest.json")
    assert r.view == "entity"
    assert r.kind is Kind.ARTIFACT
    assert r.entity_id == "ops/checks/comp-alpha/latest.json"


def test_history_route_is_addressable_with_its_requested_window():
    r = resolve("/history/component/comp-alpha", "window_hours=48")
    assert (r.view, r.kind, r.entity_id, r.window_hours) == (
        "history", Kind.COMPONENT, "comp-alpha", 48,
    )


# ------------------------------------------------------- CN-4.1 depth budget

#: The views the resolver may produce. Adding one is adding a level unless it
#: is an entity tab (today: history). Enumerated here so a new view fails
#: this file rather than silently widening §4.1 (alpha-engine-config-I7422).
_ALLOWED_VIEWS = {
    "landing", "list", "entity", "search", "doctor", "registry", "history",
}


def test_every_resolved_view_is_inside_the_three_tier_budget():
    """Enumerate, do not hand-list routes. A fourth-level view is a new
    `view=` that is not an entity page or its own tab."""
    routes = ["/", "/search", "/doctor/x", "/registry/fleet"]
    routes += [f"/{k.route}" for k in Kind]
    routes += [f"/{k.route}/some-id" for k in Kind]
    routes += [f"/history/{k.route}/some-id" for k in Kind]
    seen = set()
    for route in routes:
        req = resolve(route)
        seen.add(req.view)
        assert req.view in _ALLOWED_VIEWS, f"{route} resolved to {req.view!r}"
    assert "landing" in seen and "list" in seen and "entity" in seen


def test_extra_segments_after_a_kind_are_the_entity_id_never_another_view():
    """The load-bearing half of §4.1: /<kind>/<a>/<b> is still the entity
    page, with id a/b. A fourth-level view would steal that path."""
    for kind in Kind:
        req = resolve(f"/{kind.route}/a/b/c")
        assert req.view == "entity", kind
        assert req.entity_id == "a/b/c", kind


def test_history_extra_segments_stay_the_entity_tab():
    req = resolve("/history/component/a/b")
    assert req.view == "history"
    assert req.entity_id == "a/b"
