"""A source document's markdown, rendered as HTML that cannot carry markup in.

`body: markdown` (s3-records, `format: object`) marks a carried document as
markdown, so its entity page shows headings, tables, lists, links and code
rather than the raw text. The document is written by a source the console does
not control, so the rendering is closed by construction rather than cleaned
after the fact:

- **Raw HTML is off.** A ``<script>`` or ``<img onerror=…>`` in the text is
  emitted as escaped text, never as a tag. There is no sanitiser to get wrong
  because nothing the author wrote is ever passed through as markup.
- **Links are http, https or relative.** Any other scheme (``javascript:``,
  ``data:``, ``vbscript:``, ``file:``, ``mailto:``…) is refused: the parser
  does not form a link at all, and the source text renders as escaped text. The check runs on the URL after markdown-it's
  normalisation, so an entity- or whitespace-obfuscated scheme is seen as the
  scheme it decodes to.
- **Images are off.** A report embedding an image would have the reader's
  browser fetch a URL the author chose; ``![alt](url)`` renders as ``!`` and
  an ordinary (equally checked) link instead.

GFM tables and strikethrough are on, because the reports this was built for
use tables throughout. ``markdown-it-py`` is a CommonMark-compliant parser
with no runtime dependencies beyond ``mdurl``; when it is not importable the
caller falls back to the escaped preformatted text (`render()` returns None),
so a missing library degrades the page rather than breaking it.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

#: A URL scheme, per RFC 3986 §3.1, at the start of a (normalised) link.
_SCHEME = re.compile(r"^([a-z][a-z0-9+.\-]*):", re.IGNORECASE)
#: The only schemes a rendered document may link to. Scheme-less (relative,
#: fragment, or protocol-relative ``//host``) links are allowed too.
SAFE_SCHEMES = frozenset({"http", "https"})


def safe_link(url: str) -> bool:
    """True when `url` may be an ``href``: http(s) or relative, nothing else."""
    m = _SCHEME.match(url.strip())
    return m is None or m.group(1).lower() in SAFE_SCHEMES


def _table_open(self: Any, tokens: list, idx: int, options: Any, env: Any) -> str:
    # A wide table scrolls inside its own box, never the page (phones).
    return '<div class="md-table">' + self.renderToken(tokens, idx, options, env)


def _table_close(self: Any, tokens: list, idx: int, options: Any, env: Any) -> str:
    return self.renderToken(tokens, idx, options, env) + "</div>"


@lru_cache(maxsize=1)
def _parser() -> Any:
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])
    md.disable("image")
    md.validateLink = safe_link
    md.add_render_rule("table_open", _table_open)
    md.add_render_rule("table_close", _table_close)
    return md


def render(text: str) -> str | None:
    """`text` as safe HTML, or None when no markdown renderer is installed."""
    try:
        md = _parser()
    except ImportError:
        return None
    return md.render(text)
