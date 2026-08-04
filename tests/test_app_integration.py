"""Integration tests — a cold URL load reconstructs the view over real HTTP.

These run the actual server on an ephemeral loopback port and fetch URLs with
no prior client state, asserting the §3.2 round-trip end to end: router →
index → render produces the addressed entity, list, landing and search views.
"""
from __future__ import annotations

import threading
import urllib.request

import pytest

from console.index.graph import Index
from console.model.envelope import AdapterResult, AdapterStatus
from console.server.app import serve
from tests.fixtures import fixture_graph


@pytest.fixture()
def live_server():
    entities, edges = fixture_graph()
    idx = Index()
    idx.add_result(AdapterResult(name="f", status=AdapterStatus.OK,
                                 entities=tuple(entities), edges=tuple(edges)))
    server = serve(idx, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


def _get(url: str) -> str:
    with urllib.request.urlopen(url) as resp:
        return resp.read().decode("utf-8")


def test_entity_page_cold_load(live_server):
    body = _get(f"{live_server}/component/comp-producer")
    assert "comp-producer" in body
    assert "component" in body


def test_entity_page_shows_derived_reverse_relation(live_server):
    # The artifact page names its producer and consumer — the reverse edge
    # that exists only because the index derives it (§3.3).
    art_url = f"{live_server}/artifact/s3://fixture-bucket/data/x.json"
    body = _get(art_url)
    assert "comp-producer" in body
    assert "comp-consumer" in body


def test_list_view_with_facet_in_url(live_server):
    body = _get(f"{live_server}/component")
    assert "comp-producer" in body


def test_landing_is_exception_first(live_server):
    body = _get(live_server + "/")
    assert "exceptions" in body
    assert "index reachability" in body


def test_stylesheet_is_served_with_state_selectors(live_server):
    body = _get(live_server + "/styles.css")
    assert ".state-HEALTHY" in body
    assert ".state-FAILED" in body


def test_html_pages_link_the_served_stylesheet(live_server):
    assert 'href="/styles.css"' in _get(live_server + "/")


def test_search_resolves_identifier(live_server):
    body = _get(f"{live_server}/search?q=comp-producer")
    assert "comp-producer" in body


def test_unknown_entity_is_404_not_blank(live_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"{live_server}/component/no-such-thing")
    assert exc.value.code == 404


def test_unknown_route_is_404(live_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"{live_server}/widgets/abc")
    assert exc.value.code == 404
