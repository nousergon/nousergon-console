"""/index: one table of contents of every console page (Brian 2026-11-04)."""
from __future__ import annotations

import pytest

from console.index import pages
from console.index.graph import Index
from console.render import json as render_json
from console.server.app import _html
from console.server.router import resolve

SEC = [{"type": "text", "title": "t", "body": "x"}]


def _index(decl):
    idx = Index()
    idx.finalize()
    pages.attach(idx, pages.parse(decl))
    return idx


def _decl():
    return {
        "perf": {"title": "Performance", "question": "are we beating SPY", "section": "trading",
                 "sections": SEC},
        "spend": {"title": "Spend", "question": "what did we spend", "section": "cost",
                  "sections": SEC},
        "misc": {"title": "Misc", "question": "anything else", "sections": SEC},
    }


def test_index_and_dashboards_are_the_same_page():
    idx = _index(_decl())
    assert _html(idx, resolve("/index")) == _html(idx, resolve("/dashboards"))


def test_pages_group_by_section_with_other_last():
    html = _html(_index(_decl()), resolve("/index"))
    order = [html.index(f'id="pages-{s}"') for s in ("cost", "trading", "other")]
    assert order == sorted(order)
    trading = html.split('id="pages-trading"', 1)[1].split("<h2", 1)[0]
    assert '<a href="/page/perf">Performance</a>' in trading
    assert '<a href="/page/misc">Misc</a>' in html.split('id="pages-other"', 1)[1]


def test_built_in_pages_are_listed_first():
    html = _html(_index(_decl()), resolve("/index"))
    start = html.split('id="start"', 1)[1].split("<h2", 1)[0]
    for url in ("/", "/exceptions", "/planner", "/reports", "/search"):
        assert f'<a href="{url}">' in start, url


def test_the_filter_keeps_matching_entries_and_drops_empty_groups():
    html = _html(_index(_decl()), resolve("/index", "q=spy"))
    assert '<a href="/page/perf">Performance</a>' in html
    assert "/page/spend" not in html and 'id="pages-cost"' not in html
    assert "1 of " in html and 'value="spy"' in html


def test_a_filter_matching_nothing_says_so():
    html = _html(_index(_decl()), resolve("/index", "q=zzzz"))
    assert "no page matches" in html


def test_the_menu_names_the_index():
    html = _html(_index(_decl()), resolve("/"))
    nav = html[html.index('<nav class="site-nav">'):]
    assert '<a href="/index">index</a>' in nav[:nav.index("</nav>")]


def test_json_carries_each_pages_section():
    doc = render_json.payload(_index(_decl()), resolve("/index"))
    assert {p["id"]: p["section"] for p in doc["pages"]} == {
        "perf": "trading", "spend": "cost", "misc": "other"}


@pytest.mark.parametrize("bad", ["Trading!", "x" * 41, "<b>"])
def test_a_bad_section_is_refused(bad):
    with pytest.raises(pages.PagesConfigError):
        pages.parse({"p": {"title": "t", "question": "q", "section": bad, "sections": SEC}})


# --- products (Brian 2026-11-05: product pages grouped, then org-wide) -----

from console.index import products  # noqa: E402


def _product_index(page_decl, product_decl):
    idx = Index()
    idx.finalize()
    layouts = pages.parse(page_decl)
    declared = products.parse(product_decl)
    products.check_pages(declared, layouts)
    pages.attach(idx, layouts)
    products.attach(idx, declared)
    return idx


def test_product_pages_group_before_org_wide_pages():
    decl = _decl()
    decl["perf"]["product"] = "crucible"
    html = _html(_product_index(decl, {
        "crucible": {"title": "Crucible", "about": "the trading system"},
        "metron": {"title": "Metron",
                   "links": [{"title": "Metron dash", "url": "https://metron.example/dash"}]},
    }), resolve("/index"))
    crucible = html.split('id="product-crucible"', 1)[1].split("<h2", 1)[0]
    assert "Crucible — the trading system" in crucible
    assert '<a href="/page/perf">Performance</a>' in crucible
    metron = html.split('id="product-metron"', 1)[1].split("<h2", 1)[0]
    assert '<a href="https://metron.example/dash">Metron dash</a>' in metron
    assert html.index('id="product-crucible"') < html.index('id="product-metron"') \
        < html.index('id="pages-cost"')
    # The product's page is not listed again under org-wide.
    assert 'id="pages-trading"' not in html
    assert "Nous Ergon · org-wide · cost" in html


def test_a_page_naming_an_undeclared_product_fails_the_build():
    decl = _decl()
    decl["perf"]["product"] = "vires"
    with pytest.raises(products.ProductsConfigError, match="not declared"):
        _product_index(decl, {"crucible": {"title": "Crucible"}})


def test_a_pane_claimed_by_two_products_fails():
    with pytest.raises(products.ProductsConfigError, match="already claimed"):
        products.parse({"a": {"title": "A", "panes": ["cost"]},
                        "b": {"title": "B", "panes": ["cost"]}})


def test_org_wide_is_reserved():
    with pytest.raises(products.ProductsConfigError):
        products.parse({"org-wide": {"title": "x"}})


def test_a_filter_matches_registries_by_name(monkeypatch):
    # I13239, codex 2ecd5dda519d6286: any filter used to hide every registry,
    # including the one it named.
    from console.index import landing

    regs = [{"name": "artifact-registry", "url": "/registry/artifacts", "status": "ok",
             "rows": 3, "declared": 3, "not_healthy": 0},
            {"name": "observation-registry", "url": "/registry/obs", "status": "ok",
             "rows": 1, "declared": 1, "not_healthy": 0}]
    monkeypatch.setattr(landing, "registry_links", lambda index: regs)
    idx = _index(_decl())
    html = _html(idx, resolve("/index", "q=artifact-registry"))
    assert 'id="registries"' in html and "/registry/artifacts" in html
    assert "/registry/obs" not in html and "1 of " in html
    assert 'href="#registries"' in html
    assert 'id="registries"' not in _html(idx, resolve("/index", "q=spy"))
    assert "/registry/obs" in _html(idx, resolve("/index"))
