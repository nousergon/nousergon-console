"""Every navigating link on the console opens a new tab (Brian, 2026-11-05).

`with_new_tab_links` is applied once, in the server's HTML send path, so these
assert both the transform's rules and that a served page actually carries them.
"""
from __future__ import annotations

import re
import threading
import urllib.request

import pytest

from console.index.graph import Index
from console.model.envelope import AdapterResult, AdapterStatus
from console.render.html import with_new_tab_links
from console.server.app import serve
from tests.fixtures import fixture_graph

NEW_TAB = 'target="_blank" rel="noopener noreferrer"'


@pytest.mark.parametrize("href", [
    "/component/x", "/", "https://github.com/o/r/pull/1", "?sort=age", "page?x=1#frag",
])
def test_navigating_link_opens_in_a_new_tab(href):
    out = with_new_tab_links(f'<p><a href="{href}">x</a></p>')
    assert out == f'<p><a href="{href}" {NEW_TAB}>x</a></p>'


@pytest.mark.parametrize("anchor", [
    '<a href="#tier-P0">P0</a>',
    '<a href="  #top">top</a>',
    '<a href="javascript:void(0)">js</a>',
    '<a href="JavaScript:void(0)">js</a>',
    '<a href="mailto:a@example.com">mail</a>',
    '<a href="tel:+1555">call</a>',
    '<a name="anchor-only">no href</a>',
    '<a href="/x" target="_self">kept</a>',
    '<a target="frame" href="/x">kept</a>',
])
def test_same_page_and_explicit_target_links_are_left_alone(anchor):
    assert with_new_tab_links(anchor) == anchor


def test_forms_buttons_and_lookalike_tags_are_untouched():
    page = ('<form method="post" action="/x"><button type="submit">go</button></form>'
            '<abbr title="t">t</abbr><article></article><aside></aside>'
            '<link rel="stylesheet" href="/styles.css">')
    assert with_new_tab_links(page) == page


def test_existing_rel_keeps_its_tokens_and_gains_noopener():
    out = with_new_tab_links('<a class="sort" rel="nofollow noopener" href="/x">x</a>')
    assert out == ('<a class="sort" rel="nofollow noopener noreferrer" href="/x" '
                   'target="_blank">x</a>')


def test_single_quoted_and_svg_anchors_are_covered():
    out = with_new_tab_links("<svg><a href='/planner/p'><text>p</text></a></svg>")
    assert f"<a href='/planner/p' {NEW_TAB}>" in out


@pytest.fixture()
def live_server():
    entities, edges = fixture_graph()
    idx = Index()
    idx.add_result(AdapterResult(name="f", status=AdapterStatus.OK,
                                 entities=tuple(entities), edges=tuple(edges)))
    server = serve(idx, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("path", ["/", "/component/comp-producer", "/component", "/exceptions",
                                  "/health", "/index"])
def test_every_served_link_opens_a_new_tab_and_anchors_do_not(live_server, path):
    with urllib.request.urlopen(f"{live_server}{path}") as resp:
        body = resp.read().decode("utf-8")
    anchors = re.findall(r"<a\b[^>]*>", body)
    navigating = [a for a in anchors
                  if re.search(r'href="(?!#|javascript:|mailto:)', a)]
    in_page = [a for a in anchors if 'href="#' in a]
    assert navigating, f"{path} rendered no links to check"
    for a in navigating:
        assert 'target="_blank"' in a and "noopener" in a and "noreferrer" in a, a
    for a in in_page:
        assert "target=" not in a, a
