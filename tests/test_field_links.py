"""Declared field values are clickable (alpha-engine-config-I11805).

A milestone's `tracker` was declared `render: link` and still rendered as a
60-character URL in plain text, and its `graded_by` sentence cited
``alpha-engine-config-I10793`` as text a reader had to copy into a browser.
Citations were linked inside markdown documents only. Now a declared field's
value is linked the same way on the entity page and in every list or
dashboard column:

- a `render: link` field is one link, read as its citation
  (``repo-I12``) when it points at a declared repository's issue or pull
  request, with the full address in the ``title``;
- every other value links its bare URLs and declared citations, as written.

The negative cases matter as much: a value that is not an http(s) address is
never made a link, markup in a value stays escaped, and nothing changes in the
JSON representation, which carries the values exactly as emitted.
"""
from __future__ import annotations

import re

import pytest

from console.index import citations
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import html as render_html
from console.render import markdown
from console.render.html import fields_section

BASE = "https://git.example/org"
LINKS = citations.parse({
    "base_url": BASE,
    "repos": ["alpha-engine-config", "nous-ergon-ops"],
    "aliases": {"ops": "nous-ergon-ops"},
})


def _entity(fields: dict, id: str = "milestone:x") -> Entity:
    return Entity(kind=Kind.COMPONENT, id=id, state=State.HEALTHY,
                  provenance=Provenance(source="test"), detail={"fields": fields})


def _anchors(html: str) -> list[tuple[str, str]]:
    """(opening-tag attributes, link text) for every link in `html`."""
    return re.findall(r"<a ([^>]*)>(.*?)</a>", html)


# ------------------------------------------------------- render: link --

@pytest.mark.parametrize("url, text", [
    (f"{BASE}/alpha-engine-config/issues/10793", "alpha-engine-config-I10793"),
    (f"{BASE}/nous-ergon-ops/pull/12", "nous-ergon-ops-PR12"),
    (f"{BASE}/nous-ergon-ops/pull/12#issuecomment-34", "nous-ergon-ops-PR12 comment 34"),
    (f"{BASE}/alpha-engine-config/issues/7/", "alpha-engine-config-I7"),
])
def test_a_link_field_to_a_declared_repository_reads_as_its_citation(url, text):
    html = fields_section(_entity({"tracker": {"value": url, "render": "link"}}), LINKS)
    assert _anchors(html) == [(f'href="{url}" title="{url}"', text)]


@pytest.mark.parametrize("url", [
    "https://example.org/dashboards/spend",
    f"{BASE}/undeclared-repo/issues/1",       # under the base, repo not declared
    "https://elsewhere.example/org/alpha-engine-config/issues/1",  # another base
    "/run/report:2026-10-15:daily",            # a path on this site
])
def test_any_other_link_field_links_to_itself(url):
    html = fields_section(_entity({"u": {"value": url, "render": "link"}}), LINKS)
    assert _anchors(html) == [(f'href="{url}"', url)]


def test_a_link_field_reads_as_itself_when_no_citations_are_declared():
    url = f"{BASE}/alpha-engine-config/issues/10793"
    html = fields_section(_entity({"tracker": {"value": url, "render": "link"}}))
    assert _anchors(html) == [(f'href="{url}"', url)]


@pytest.mark.parametrize("value", [
    "s3://bucket/key/report.json",
    "javascript:alert(1)",
    "data:text/html,<script>x</script>",
    "//evil.example/x",
    "not an address",
])
def test_a_link_field_that_is_not_an_http_address_is_never_a_link(value):
    html = fields_section(_entity({"evidence": {"value": value, "render": "link"}}), LINKS)
    assert "<a " not in html
    assert "<script>" not in html


def test_a_link_field_holding_prose_links_only_what_is_inside_it():
    value = "filed as ops#5, see https://example.org/x."
    html = fields_section(_entity({"evidence": {"value": value, "render": "link"}}), LINKS)
    assert _anchors(html) == [(f'href="{BASE}/nous-ergon-ops/issues/5"', "ops#5"),
                              ('href="https://example.org/x"', "https://example.org/x")]


# --------------------------------------------------------- text values --

def test_citations_and_urls_in_a_text_field_become_links_as_written():
    value = ("data-phase1 row alpha-engine-config-I10793 on this pane, "
             "fixed in ops-PR44 (https://example.org/run/1).")
    html = fields_section(_entity({"graded_by": {"value": value, "render": "text"}}), LINKS)
    assert _anchors(html) == [
        (f'href="{BASE}/alpha-engine-config/issues/10793"', "alpha-engine-config-I10793"),
        (f'href="{BASE}/nous-ergon-ops/pull/44"', "ops-PR44"),
        ('href="https://example.org/run/1"', "https://example.org/run/1"),
    ]
    # The words around the links are kept, in order.
    assert "data-phase1 row " in html and " on this pane, fixed in " in html


def test_without_a_declaration_urls_link_and_references_stay_text():
    value = "alpha-engine-config-I10793, https://example.org/x"
    html = fields_section(_entity({"graded_by": {"value": value, "render": "text"}}))
    assert _anchors(html) == [('href="https://example.org/x"', "https://example.org/x")]
    assert "alpha-engine-config-I10793" in html


def test_markup_in_a_value_stays_escaped_around_its_links():
    value = '<img src=x onerror=alert(1)> ops#9 "quoted" & more'
    html = fields_section(_entity({"note": {"value": value, "render": "text"}}), LINKS)
    assert "<img" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "&quot;quoted&quot; &amp; more" in html
    assert _anchors(html) == [(f'href="{BASE}/nous-ergon-ops/issues/9"', "ops#9")]


def test_an_undeclared_bare_value_is_still_opaque_but_its_urls_link():
    html = fields_section(_entity({"raw": "see https://example.org/y"}), LINKS)
    assert "not fully declared" in html
    assert _anchors(html) == [('href="https://example.org/y"', "https://example.org/y")]


@pytest.mark.parametrize("spec, shown", [
    ({"value": 10793, "render": "count", "unit": "rows", "baseline": None}, "10,793"),
    ({"value": 1.5, "render": "ratio", "unit": "x", "baseline": None}, "1.5"),
])
def test_numbers_render_exactly_as_before(spec, shown):
    html = fields_section(_entity({"n": spec}), LINKS)
    assert "<a " not in html and shown in html


def test_link_text_never_needs_the_markdown_library():
    # `link_text` is a pure function over the same `_links` pass; it is used
    # where markdown is not, so it must not import markdown-it.
    assert markdown.link_text("a <b> ops#1", LINKS) == (
        f'a &lt;b&gt; <a href="{BASE}/nous-ergon-ops/issues/1">ops#1</a>')


def test_reference_for_is_the_inverse_of_the_url_builders():
    for url, ref in [(LINKS.issue_url("nous-ergon-ops", "3"), "nous-ergon-ops-I3"),
                     (LINKS.pull_url("nous-ergon-ops", "4", "5"), "nous-ergon-ops-PR4 comment 5")]:
        assert LINKS.reference_for(url) == ref
    assert LINKS.reference_for(f"{BASE}/nous-ergon-ops/tree/main") is None


# ------------------------------------------------------------ the pages --

def _index(*ents: Entity) -> Index:
    index = Index()
    index.add_result(AdapterResult(name="milestones", status=AdapterStatus.OK,
                                   claim_class=ClaimClass.OBSERVATION, entities=ents))
    index.finalize()
    citations.attach(index, LINKS)
    return index


FIELDS = {
    "tracker": {"value": f"{BASE}/alpha-engine-config/issues/10793", "render": "link"},
    "graded_by": {"value": "phase4 row ops#9760 on this pane", "render": "text"},
}
TRACKER = (f'<a href="{BASE}/alpha-engine-config/issues/10793" '
           f'title="{BASE}/alpha-engine-config/issues/10793">alpha-engine-config-I10793</a>')
GRADED = f'<a href="{BASE}/nous-ergon-ops/issues/9760">ops#9760</a>'


def test_the_entity_page_and_a_dashboard_column_both_link_field_values():
    ent = _entity(FIELDS)
    index = _index(ent)
    entity = render_html.entity_page(index, ent)
    listing = render_html.list_page(index, Kind.COMPONENT, {})
    for page in (entity, listing):
        assert TRACKER in page
        assert GRADED in page


def test_the_json_representation_carries_the_values_as_emitted():
    from console.render import json as render_json
    from console.server.router import Resolved

    ent = _entity(FIELDS)
    index = _index(ent)
    doc = render_json.dumps(render_json.payload(
        index, Resolved(view="entity", kind=Kind.COMPONENT, entity_id=ent.id)))
    assert f"{BASE}/alpha-engine-config/issues/10793" in doc
    assert "phase4 row ops#9760 on this pane" in doc
    assert "<a " not in doc
