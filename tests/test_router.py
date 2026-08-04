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
