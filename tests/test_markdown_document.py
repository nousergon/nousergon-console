"""A document declared markdown (`body: markdown`) renders as HTML, safely.

The reports this was built for (the Director's evening plans, morning reports
and run summaries) use headings, GFM tables, lists, bold, code and links. The
text comes from a source the console does not control, so the tests that
matter most here are the ones proving nothing the author wrote reaches the page
as markup: raw HTML is shown as text, and only http(s) or relative links
survive.
"""
from __future__ import annotations

import builtins

import pytest

from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import html as render_html
from console.render import markdown

REPORT = """# Evening plan 2026-10-05

Plan for **tomorrow**, see [the run](https://example.com/run/1) and [home](/).

## Steps

- one with `code`
- two ~~dropped~~

| step | owner | state |
|---|:---:|---:|
| fetch | data | done |
| score | research | queued |

```
raw block <b>
```
"""


def _page(text: str, fmt: str | None = "markdown", **doc) -> str:
    document = {"text": text, "bytes": len(text.encode()), "truncated": False, **doc}
    if fmt is not None:
        document["format"] = fmt
    ent = Entity(kind=Kind.RUN, id="report:d-0:evening-plan", state=State.HEALTHY,
                 provenance=Provenance(source="s3://b/r/", as_of="2026-10-05T22:00:00+00:00",
                                       evidence="s3://b/r/plan.md"),
                 facets={"pane": "reports"}, detail={"document": document})
    index = Index()
    index.add_result(AdapterResult(name="f", status=AdapterStatus.OK,
                                   claim_class=ClaimClass.OBSERVATION, entities=(ent,)))
    index.finalize()
    return render_html.entity_page(index, ent)


# --------------------------------------------------------------- rendering --

def test_a_markdown_document_renders_headings_lists_tables_and_code():
    page = _page(REPORT)
    assert '<div class="document markdown">' in page
    assert '<pre class="document">' not in page
    assert "<h1>Evening plan 2026-10-05</h1>" in page
    assert "<h2>Steps</h2>" in page
    assert "<strong>tomorrow</strong>" in page
    assert "<li>one with <code>code</code></li>" in page
    assert "<s>dropped</s>" in page
    assert '<div class="md-table"><table>' in page and "</table>\n</div>" in page
    assert "<th>step</th>" in page and "<td>score</td>" in page
    assert "<td style=\"text-align:right\">queued</td>" in page
    assert '<th style="text-align:center">owner</th>' in page
    assert '<a href="https://example.com/run/1">the run</a>' in page
    assert '<a href="/">home</a>' in page
    # A fenced block is code, and its contents are text.
    assert "<pre><code>raw block &lt;b&gt;\n</code></pre>" in page


def test_a_plain_text_document_is_still_preformatted_and_escaped():
    for fmt in ("text", None):
        page = _page("# not a heading <b>", fmt=fmt)
        assert '<pre class="document"># not a heading &lt;b&gt;</pre>' in page
        assert "<h1>not a heading" not in page


# ---------------------------------------------------------------- escaping --

@pytest.mark.parametrize("hostile", [
    "<script>alert(1)</script>",
    '<img src=x onerror="alert(1)">',
    "<iframe src=https://evil.example></iframe>",
    "<div>\n<script>alert(1)</script>\n</div>",
    "<a href=\"javascript:alert(1)\">x</a>",
])
def test_raw_html_in_the_document_is_shown_as_text_never_as_markup(hostile):
    page = _page(f"# Report\n\nbefore\n\n{hostile}\n\nafter *ok*")
    doc = page.split('<div class="document markdown">', 1)[1].split("</div>\n<h2>", 1)[0]
    assert "<script" not in doc
    assert "<img" not in doc
    assert "<iframe" not in doc
    assert '<a href="javascript' not in doc
    assert "&lt;" in doc  # the author's markup is there, as text
    assert "<em>ok</em>" in doc  # and the markdown around it still renders


def test_html_escapes_inside_table_cells_and_code():
    page = _page("| a |\n|---|\n| <script>x</script> |\n\n`<b>`")
    assert "<td>&lt;script&gt;x&lt;/script&gt;</td>" in page
    assert "<code>&lt;b&gt;</code>" in page
    assert "<script>" not in page


# ------------------------------------------------------------------- links --

@pytest.mark.parametrize("dest", [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "&#106;avascript:alert(1)",
    "vbscript:msgbox(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "file:///etc/passwd",
    "mailto:someone@example.com",
])
def test_a_link_to_any_other_scheme_is_not_a_link(dest):
    page = _page(f"see [the thing]({dest}) now")
    doc = page.split('<div class="document markdown">', 1)[1].split("</div>", 1)[0]
    # No anchor is formed; the source shows as (escaped) text.
    assert "<a " not in doc
    assert doc.startswith("<p>see [the thing](")


def test_autolinks_and_reference_links_obey_the_same_rule():
    page = _page("<javascript:alert(1)> <https://ok.example/>\n\n[r][x]\n\n"
                 "[x]: javascript:alert(1)\n")
    doc = page.split('<div class="document markdown">', 1)[1].split("</div>", 1)[0]
    assert '<a href="https://ok.example/">https://ok.example/</a>' in doc
    assert 'href="javascript' not in doc
    assert "&lt;javascript:alert(1)&gt;" in doc


def test_images_are_not_embedded():
    page = _page("![chart](https://evil.example/pixel.png)")
    assert "<img" not in page


@pytest.mark.parametrize("url, ok", [
    ("https://example.com", True), ("http://example.com", True),
    ("HTTPS://EXAMPLE.COM", True), ("/run/x", True), ("#top", True),
    ("../up", True), ("//example.com/x", True), ("plain", True),
    ("javascript:alert(1)", False), (" javascript:x", False),
    ("data:text/plain,x", False), ("ftp://x", False), ("mailto:x@y", False),
])
def test_safe_link(url, ok):
    assert markdown.safe_link(url) is ok


# -------------------------------------------------------- cut and fallback --

def test_a_cut_markdown_document_says_so_and_still_renders():
    page = _page("## Head\n\n| a |\n|---|\n| 1 |", truncated=True, bytes=900000)
    assert "document cut" in page and "900000 bytes" in page
    assert page.index("document cut") < page.index('<div class="document markdown">')
    assert "<h2>Head</h2>" in page


def test_without_a_markdown_renderer_the_text_is_shown_escaped_and_says_so(monkeypatch):
    markdown._parser.cache_clear()
    real_import = builtins.__import__

    def no_markdown_it(name, *args, **kwargs):
        if name == "markdown_it" or name.startswith("markdown_it."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_markdown_it)
    try:
        assert markdown.render("# x") is None
        page = _page("# x <script>")
    finally:
        monkeypatch.undo()
        markdown._parser.cache_clear()
    assert "markdown renderer unavailable" in page
    assert '<pre class="document"># x &lt;script&gt;</pre>' in page
    assert markdown.render("# x") == "<h1>x</h1>\n"
