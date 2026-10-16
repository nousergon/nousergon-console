"""Citation links — plain-text references in a rendered markdown document
become links to the repository they name, at render time, and nothing else does.

The tests that matter most are the negative ones: an undeclared name, a word
glued to a reference, a bare ``#N`` with no repository, and anything inside a
link, code span or code block must stay exactly as written, and every href the
linker produces must pass the renderer's own scheme check.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from console.index import citations
from console.index.citations import CitationsConfigError
from console.render import markdown

BASE = "https://git.example/org"
DECL = {
    "base_url": BASE + "/",
    "repos": ["alpha-engine-config", "nous-ergon-ops", "nousergon-data",
              "metron-ops", "crucible-research", "flow-doctor", ".github"],
    "aliases": {"aec": "alpha-engine-config", "ops": "nous-ergon-ops",
                "data": "nousergon-data", "research": "crucible-research"},
}
LINKS = citations.parse(DECL)


def html(text: str, links=LINKS) -> str:
    out = markdown.render(text, links)
    assert out is not None
    return out


def hrefs(text: str, links=LINKS) -> list[tuple[str, str]]:
    """(href, link text) for every link in the rendered output."""
    return re.findall(r'<a href="([^"]*)">(.*?)</a>', html(text, links))


# ------------------------------------------------------------- patterns --

@pytest.mark.parametrize("text, href", [
    ("see ops#1709 today", f"{BASE}/nous-ergon-ops/issues/1709"),
    ("aec#12351", f"{BASE}/alpha-engine-config/issues/12351"),
    ("data#2176.", f"{BASE}/nousergon-data/issues/2176"),
    ("research#859", f"{BASE}/crucible-research/issues/859"),
    ("flow-doctor#113", f"{BASE}/flow-doctor/issues/113"),
    ("nousergon-data#2176", f"{BASE}/nousergon-data/issues/2176"),
    ("metron-ops#361", f"{BASE}/metron-ops/issues/361"),
    (".github#7", f"{BASE}/.github/issues/7"),
    ("alpha-engine-config-I12351", f"{BASE}/alpha-engine-config/issues/12351"),
    ("aec-I12351", f"{BASE}/alpha-engine-config/issues/12351"),
    ("nous-ergon-ops-PR1718", f"{BASE}/nous-ergon-ops/pull/1718"),
    ("ops-PR1718", f"{BASE}/nous-ergon-ops/pull/1718"),
])
def test_each_reference_form_links_to_its_repository(text, href):
    assert [h for h, _ in hrefs(text)] == [href]


def test_link_text_is_the_reference_as_written():
    assert hrefs("(ops#1709)") == [(f"{BASE}/nous-ergon-ops/issues/1709", "ops#1709")]


@pytest.mark.parametrize("text, href, label", [
    ("nousergon-data#2110 comment 6213967618",
     f"{BASE}/nousergon-data/issues/2110#issuecomment-6213967618",
     "nousergon-data#2110 comment 6213967618"),
    ("metron-ops#361 comment 6207011189).",
     f"{BASE}/metron-ops/issues/361#issuecomment-6207011189",
     "metron-ops#361 comment 6207011189"),
    ("aec-I12 comment 99", f"{BASE}/alpha-engine-config/issues/12#issuecomment-99",
     "aec-I12 comment 99"),
    ("ops-PR5 comment 77", f"{BASE}/nous-ergon-ops/pull/5#issuecomment-77",
     "ops-PR5 comment 77"),
])
def test_a_following_comment_links_to_the_comment(text, href, label):
    assert hrefs(text) == [(href, label)]


def test_a_comment_not_right_after_a_reference_is_text():
    assert hrefs("see comment 6213967618") == []
    assert hrefs("ops#1, then comment 5") == [(f"{BASE}/nous-ergon-ops/issues/1", "ops#1")]


def test_a_bare_number_continuing_a_list_inherits_the_repository():
    got = hrefs("data#2176, #2177 and #2178 / #2179 + #2180 or #2181 & #2182; #2183")
    assert got == [(f"{BASE}/nousergon-data/issues/{n}", f"{'data' if n == 2176 else ''}#{n}")
                   for n in range(2176, 2183)]


def test_longest_name_wins_over_an_alias_inside_it():
    # `nousergon-data` must not be read as `nousergon-` + alias `data`.
    assert hrefs("nousergon-data#1") == [(f"{BASE}/nousergon-data/issues/1",
                                          "nousergon-data#1")]


def test_references_in_emphasis_headings_lists_and_tables_link():
    text = ("# ops#1\n\n- **aec#2**\n- _data#3_\n\n| a |\n|---|\n| research#4 |\n")
    assert [h for h, _ in hrefs(text)] == [
        f"{BASE}/nous-ergon-ops/issues/1", f"{BASE}/alpha-engine-config/issues/2",
        f"{BASE}/nousergon-data/issues/3", f"{BASE}/crucible-research/issues/4"]


# ------------------------------------------------------------- bare URLs --

@pytest.mark.parametrize("text, url", [
    ("see https://github.com/o/r/pull/1709 now", "https://github.com/o/r/pull/1709"),
    ("https://x.example/a/issues/1#issuecomment-2 .", "https://x.example/a/issues/1#issuecomment-2"),
    ("(see https://x.example/a).", "https://x.example/a"),
    ("https://x.example/wiki/A_(b)", "https://x.example/wiki/A_(b)"),
    ("http://x.example/q?a=1&b=2, then", "http://x.example/q?a=1&amp;b=2"),
])
def test_bare_urls_become_links(text, url):
    assert hrefs(text) == [(url, url)]


def test_bare_urls_link_without_a_citations_declaration():
    assert hrefs("https://x.example/a and ops#1", links=None) == [
        ("https://x.example/a", "https://x.example/a")]


def test_a_url_containing_a_reference_shape_is_one_link():
    url = "https://github.com/o/nousergon-data/pull/2110#issuecomment-6213967618"
    assert hrefs(url) == [(url, url)]


# ------------------------------------------------------------- negatives --

@pytest.mark.parametrize("text", [
    "unknown#12",              # undeclared name
    "Ruling #25",              # no repository at all
    "merge main into #114",    # bare number with no reference before it
    "ruling-#25",
    "xops#12",                 # name glued to a preceding word
    "foo-ops#12",              # ... or a preceding dash
    "path/ops#12",             # ... or a path
    "ops#12abc",               # number glued to a following word
    "ops#",                    # no number
    "OPS#12",                  # names are case-sensitive
    "aec-i12 aec-pr12",        # the I/PR markers are case-sensitive
    "ops #12",                 # a space breaks the reference
    "javascript:alert(1)",     # not a link scheme the linker forms
    "ftp://x.example/a",
    "https://",
])
def test_things_that_are_not_references_stay_text(text):
    assert hrefs(text) == []


def test_a_list_continuation_needs_a_reference_before_it():
    assert hrefs("data collector: #2177, #2178") == []


def test_nothing_inside_an_existing_link_is_rewritten():
    got = hrefs("[ops#1 and https://a.example](https://b.example) <https://c.example/ops#2>")
    assert got == [("https://b.example", "ops#1 and https://a.example"),
                   ("https://c.example/ops#2", "https://c.example/ops#2")]


def test_code_spans_and_blocks_are_never_rewritten():
    out = html("`ops#1 https://a.example`\n\n```\nops#2 https://b.example\n```\n\n"
               "    ops#3 https://c.example\n")
    assert "<a " not in out
    assert "<code>ops#1 https://a.example</code>" in out
    assert "ops#2 https://b.example" in out and "ops#3 https://c.example" in out


def test_the_safe_renderer_guarantees_still_hold():
    out = html('ops#1 <script>alert(1)</script> [x](javascript:alert(1)) '
               'https://a.example/"><img src=x onerror=alert(1)>')
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert 'href="javascript' not in out
    assert "<img" not in out
    # The bare URL stops at the quote; nothing after it reaches an attribute.
    assert hrefs('https://a.example/"x') == [("https://a.example/", "https://a.example/")]


def test_every_generated_href_passes_the_scheme_check():
    text = "ops#1 aec-I2 ops-PR3 data#4 comment 5, #6 https://a.example http://b.example"
    for href, _ in hrefs(text):
        assert markdown.safe_link(href)


# ----------------------------------------------------------- declaration --

def test_undeclared_block_is_none_and_links_nothing():
    assert citations.parse(None) is None


def test_base_url_trailing_slash_is_dropped():
    assert LINKS.base_url == BASE


@pytest.mark.parametrize("bad, needle", [
    ([], "must be a mapping"),
    ({"repos": ["r"]}, "base_url"),
    ({"base_url": "javascript:alert(1)", "repos": ["r"]}, "base_url"),
    ({"base_url": "git.example/org", "repos": ["r"]}, "base_url"),
    ({"base_url": BASE}, "repos"),
    ({"base_url": BASE, "repos": []}, "repos"),
    ({"base_url": BASE, "repos": ["a/b"]}, "path segment"),
    ({"base_url": BASE, "repos": ["r"], "aliases": ["x"]}, "aliases"),
    ({"base_url": BASE, "repos": ["r"], "aliases": {"x": "nope"}}, "does not declare"),
    ({"base_url": BASE, "repos": ["r"], "aliases": {"a b": "r"}}, "path segment"),
    ({"base_url": BASE, "repos": ["r", "s"], "aliases": {"s": "r"}}, "shadows"),
])
def test_bad_declarations_fail_by_name(bad, needle):
    with pytest.raises(CitationsConfigError, match=re.escape(needle)):
        citations.parse(bad)


def test_the_build_fails_on_a_bad_declaration():
    from console.config import build_index

    with pytest.raises(CitationsConfigError):
        build_index({"adapters": {}, "citations": {"base_url": "ftp://x", "repos": ["r"]}})


def test_example_config_declares_citations_that_parse():
    cfg = yaml.safe_load((Path(__file__).parent.parent / "config.example.yaml").read_text())
    parsed = citations.parse(cfg["citations"])
    assert parsed is not None and parsed.repo("ex") == "example-repo"


# ------------------------------------------------------------- the pages --

def test_entity_page_and_report_feed_both_link_citations():
    from console.index.graph import Index
    from console.model.entity import Entity, Provenance
    from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
    from console.model.kinds import Kind, State
    from console.render import html as render_html
    from console.index import reports

    ent = Entity(kind=Kind.RUN, id="report:2026-10-15:daily", state=State.HEALTHY,
                 provenance=Provenance(source="s3://b/r/", as_of="2026-10-15T13:30:00+00:00",
                                       evidence="s3://b/r/2026-10-15.md"),
                 facets={"report": "daily"},
                 detail={"document": {"text": "# r\n\nops#1709 and `ops#1`",
                                      "format": "markdown"}})
    index = Index()
    index.add_result(AdapterResult(name="f", status=AdapterStatus.OK,
                                   claim_class=ClaimClass.OBSERVATION, entities=(ent,)))
    index.finalize()
    citations.attach(index, LINKS)
    reports.attach(index, reports.parse(
        {"feeds": [{"id": "daily", "title": "Daily", "facets": {"report": "daily"}}]}))
    link = f'<a href="{BASE}/nous-ergon-ops/issues/1709">ops#1709</a>'
    assert link in render_html.entity_page(index, ent)
    assert link in render_html.reports_page(index)
    assert "<code>ops#1</code>" in render_html.reports_page(index)
