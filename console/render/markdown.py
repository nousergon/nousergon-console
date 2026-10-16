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

**Citations and bare URLs become links, at render time.** A bare
``https://…`` in the text is linked as written (trailing sentence punctuation
excluded), and — when the config declares `citations:` (`index/citations.py`)
— so is every reference to a declared repository: ``repo#N`` and
``repo-IN`` to its issue, ``repo-PRN`` to its pull request, a following
``comment C`` to that comment, and a bare ``#N`` continuing a list right after
one (``data#2176, #2177``) to the same repository. This runs over the parsed
TEXT only: nothing inside an existing link, a code span or a code block is
touched, every generated href passes the same scheme check above, and the link
text is the text as written. Nothing is stored, so a document already in the
source gets links the next time it is rendered.

GFM tables and strikethrough are on, because the reports this was built for
use tables throughout. ``markdown-it-py`` is a CommonMark-compliant parser
with no runtime dependencies beyond ``mdurl``; when it is not importable the
caller falls back to the escaped preformatted text (`render()` returns None),
so a missing library degrades the page rather than breaking it.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Iterator

from ..index.citations import Citations

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
    # After `text_join`, so escaped characters and entities are already one
    # text run with their neighbours.
    md.core.ruler.push("citation_links", _link_citations)
    return md


#: A bare http(s) URL in running text. It stops at whitespace, angle
#: brackets, quotes and backticks; trailing punctuation is trimmed after.
_BARE_URL = r"https?://[^\s<>\"'`]+"
#: Characters that end a sentence around a URL rather than belong to it.
_URL_TRAILING = ".,;:!?'\"*_~"
#: A bare ``#N`` continuing a list of references (``data#2176, #2177``).
_CONTINUATION = re.compile(r"(?:\s*(?:,|/|&|\+|\band\b|\bor\b)\s*)#(?P<n>\d+)(?!\w)")
_COMMENT = r"(?:[ \t]+comment[ \t]+(?P<{0}>\d+)(?!\w))?"


@lru_cache(maxsize=8)
def _citation_pattern(citations: Citations | None) -> re.Pattern[str]:
    """One pass over a text run: a bare URL, or a reference to a declared
    repository. Longest name first, so ``nousergon-data#1`` is never read as
    the alias ``data#1``; a name glued to a preceding word, path or ``-`` is
    not a reference."""
    alternatives = [f"(?P<url>{_BARE_URL})"]
    if citations is not None:
        names = "|".join(re.escape(n) for n, _ in
                         sorted(citations.names, key=lambda kv: -len(kv[0])))
        alternatives.append(
            rf"(?<![\w.#/-])(?P<name>{names})"
            rf"(?:#(?P<hash>\d+)|-I(?P<issue>\d+)|-PR(?P<pull>\d+))(?!\w)"
            + _COMMENT.format("comment"))
    return re.compile("|".join(alternatives))


def _trim_url(url: str) -> str:
    """Drop sentence punctuation and an unbalanced closing bracket from the
    end of a bare URL: ``(see https://x/y).`` links ``https://x/y``."""
    while url:
        last = url[-1]
        if last in _URL_TRAILING:
            url = url[:-1]
        elif last == ")" and url.count("(") < url.count(")"):
            url = url[:-1]
        elif last == "]" and url.count("[") < url.count("]"):
            url = url[:-1]
        else:
            break
    return url


def _links(text: str, citations: Citations | None) -> Iterator[tuple[int, int, str]]:
    """Every (start, end, href) to link in one text run, in order."""
    pattern = _citation_pattern(citations)
    pos = 0
    while (m := pattern.search(text, pos)) is not None:
        if m.group("url"):
            url = _trim_url(m.group("url"))
            if len(url) <= len("https://"):
                pos = m.end()
                continue
            yield m.start(), m.start() + len(url), url
            pos = m.start() + len(url)
            continue
        assert citations is not None
        repo = citations.repo(m.group("name"))
        assert repo is not None
        if m.group("pull"):
            yield m.start(), m.end(), citations.pull_url(repo, m.group("pull"), m.group("comment"))
        else:
            number = m.group("hash") or m.group("issue")
            yield m.start(), m.end(), citations.issue_url(repo, number, m.group("comment"))
        pos = m.end()
        # ``data#2176, #2177 and #2178``: each bare number inherits the repo.
        while (c := _CONTINUATION.match(text, pos)) is not None:
            start = c.start("n") - 1
            yield start, c.end(), citations.issue_url(repo, c.group("n"))
            pos = c.end()


def _link_citations(state: Any) -> None:
    """Core rule: split each plain text token outside a link into text and
    links. Code spans (`code_inline`) and code blocks are other token types
    and are never visited; nothing inside an existing link is either."""
    from markdown_it.token import Token

    citations = (state.env or {}).get("citations")
    md = state.md
    for block in state.tokens:
        if block.type != "inline" or not block.children:
            continue
        out: list[Token] = []
        depth = 0
        for tok in block.children:
            if tok.type == "link_open":
                depth += 1
            elif tok.type == "link_close":
                depth -= 1
            if tok.type != "text" or depth > 0 or not tok.content:
                out.append(tok)
                continue
            text, cursor, pieces = tok.content, 0, []
            for start, end, url in _links(text, citations):
                href = md.normalizeLink(url)
                if not md.validateLink(href):
                    continue
                if start > cursor:
                    pieces.append(_text_token(Token, text[cursor:start], tok.level))
                opener = Token("link_open", "a", 1, markup="linkify", info="auto")
                opener.attrs = {"href": href}
                opener.level = tok.level
                pieces += [opener,
                           _text_token(Token, text[start:end], tok.level + 1),
                           Token("link_close", "a", -1, markup="linkify",
                                 info="auto", level=tok.level)]
                cursor = end
            if not pieces:
                out.append(tok)
                continue
            if cursor < len(text):
                pieces.append(_text_token(Token, text[cursor:], tok.level))
            out += pieces
        block.children = out


def _text_token(token_cls: Any, content: str, level: int) -> Any:
    tok = token_cls("text", "", 0, level=level)
    tok.content = content
    return tok


def render(text: str, citations: Citations | None = None) -> str | None:
    """`text` as safe HTML, or None when no markdown renderer is installed.
    Bare URLs are linked always; references to repositories `citations`
    declares are linked when it is given."""
    try:
        md = _parser()
    except ImportError:
        return None
    return md.render(text, {"citations": citations})
