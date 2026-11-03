"""Demo mode — the same pages, shown to an audience, with what is private left out.

A guided demonstration (alpha-engine-config-I13023) reuses the console's own
declared pages and sources: it never computes a second number, it only decides
what is SHOWN. It is served under one URL prefix, ``/demo/...``, so the mode is
part of the URL (§3.2: a pasted link reproduces the view, there is no session
to lose) and every link a demo page renders stays inside the prefix — a
presenter clicking through cannot fall out of demo mode by accident.

**Off unless declared.** With no ``demo:`` block, or ``enabled: false``, every
``/demo`` URL is a 404 and nothing else about the console changes. Demo mode
changes nothing about authentication: it is served by the same process behind
the same edge identity as every other view.

**Default deny, at three layers.** Each layer holds on its own, so a mistake in
one is not a leak:

1. *Routes.* Only the declared page set (``pages:``, page-id patterns) and a
   generated tour index are served, each in both representations (the same
   URL serves HTML and JSON, as every console route does — contract § API).
   Every other view — home, lists, entity pages, search, reports, planner,
   registries, the doctor — answers "not part of the demo". A link on a demo
   page that points at a route outside the set is rendered as plain text, not
   as a link, and in JSON its `url` is null.
2. *Rows and fields.* The pages are evaluated over a view of the index in which
   rows of a hidden kind or carrying a hidden facet value do not exist, and
   hidden field names are stripped from every remaining row. A selector cannot
   match what is not there, so neither can a variable picker, a tile or a
   count. Report sections (free text written by a source this module cannot
   read ahead of time) are not shown; a declared ``text`` section is, because
   it is config the demo's author wrote. The index's per-source read list is
   reduced to its build time.
3. *Text.* Every string of every demo response passes a redaction filter for the
   classes that must never reach an audience whatever a source wrote: cloud
   account ids, ARNs, access-key and token shapes, private-key blocks, e-mail
   addresses, instance ids, storage URIs, IP addresses, and any declared
   prefix (parameter-store paths). These built-in classes cannot be disabled
   by config; config can only ADD patterns.

The publication test that decides what a deployment hides (cost and spend,
personal portfolios, private strategy detail) is the deployment's: those are
facet values and field names in its own config. This module carries the
mechanism only, so nothing about any deployment lives in it.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import html as _html
import re
from dataclasses import dataclass
from typing import Any, Mapping

PREFIX = "/demo"
_ATTR = "_console_demo"
_VIEW_ATTR = "_console_demo_view"

#: The closed key set of a `demo:` block. An unknown key fails the build: a
#: misspelt `hide` would otherwise hide nothing, silently.
_KEYS = {"enabled", "title", "intro", "pages", "hide", "redact"}
_HIDE_KEYS = {"kinds", "facets", "fields"}
_REDACT_KEYS = {"prefixes", "patterns"}

#: Built-in redaction classes: (label, pattern). Always applied in demo mode;
#: config can add to them and never remove one. Each pattern excludes the HTML
#: metacharacters `<>"'` (and a backslash) so a redaction can never split a
#: tag or an attribute.
class _EmailPattern:
    """The e-mail class, matched exactly as the regex
    ``[A-Za-z0-9._%+-]+@<domain>`` would be under ``re.sub``, in linear time.

    That regex is unanchored and starts with a ``+`` run, so on a long run of
    letters with no ``@`` every start position rescans the rest of the run:
    quadratic (60 KB of letters took 6 s; CodeQL py/polynomial-redos). A match
    holds exactly one ``@`` and its local part is the class run just left of
    it, so the leftmost match is found from the ``@`` instead: walk left to
    the run start (never into the previous match, as ``re.sub`` resumes at its
    end), then match the domain anchored after the ``@``."""

    _LOCAL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._%+-")
    _DOMAIN = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")

    def sub(self, repl: str, text: str) -> str:
        out: list[str] = []
        last = 0
        at = text.find("@")
        while at >= 0:
            start = at
            while start > last and text[start - 1] in self._LOCAL:
                start -= 1
            domain = self._DOMAIN.match(text, at + 1) if start < at else None
            if domain:
                out.append(text[last:start])
                out.append(repl)
                last = domain.end()
            at = text.find("@", max(at + 1, last))
        out.append(text[last:])
        return "".join(out)


BUILTIN_REDACTIONS: tuple[tuple[str, re.Pattern[str] | _EmailPattern], ...] = tuple(
    (label, re.compile(pattern) if isinstance(pattern, str) else pattern) for label, pattern in (
        ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[^<\"']*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
        ("ARN", r"\barn:aws[a-z-]*:[^\s<>\"'\\]+"),
        ("access key", r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b"),
        ("token", r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                  r"|xox[abprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{20,})"),
        ("credential", r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*[^\s<>\"'\\]+"),
        ("e-mail", _EmailPattern()),
        ("storage path", r"\bs3a?://[^\s<>\"'\\]+"),
        ("instance id", r"\bi-(?:[0-9a-f]{17}|[0-9a-f]{8})\b"),
        ("account id", r"(?<![\w.:/-])(?:\d{12}|\d{4}-\d{4}-\d{4})(?![\w.-])"),
        ("IP address", r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"),
    )
)


class DemoConfigError(ValueError):
    """A `demo:` declaration this build cannot honour. Raised during
    `config.build_index`, so it fails the build naming the key rather than
    serving a demo that hides less than its author declared."""


@dataclass(frozen=True)
class Demo:
    enabled: bool = False
    title: str = "Guided tour"
    intro: str | None = None
    #: fnmatch patterns over declared page ids, in tour order.
    pages: tuple[str, ...] = ()
    hide_kinds: frozenset[str] = frozenset()
    #: facet name -> values; a row carrying any listed value is hidden.
    hide_facets: tuple[tuple[str, frozenset[str]], ...] = ()
    hide_fields: frozenset[str] = frozenset()
    redact_prefixes: tuple[str, ...] = ()
    redact_patterns: tuple[re.Pattern[str], ...] = ()

    # ---------------------------------------------------------------- rules --

    def page_allowed(self, page_id: str | None) -> bool:
        return bool(page_id) and any(fnmatch.fnmatchcase(page_id, p) for p in self.pages)

    def row_hidden(self, ent: Any) -> bool:
        kind = getattr(ent.kind, "value", ent.kind)
        if kind in self.hide_kinds:
            return True
        facets = ent.facets or {}
        return any(str(facets.get(name)) in values
                   for name, values in self.hide_facets if name in facets)

    def page_doc(self, pg: dict) -> dict:
        """An evaluated page with every declared mention of a hidden field
        removed: its table column, its tile, its chart. The rows already
        carry no such field (`DemoIndex`); this removes the NAME, which the
        page's own declaration would otherwise print as a header or as
        "carries no field ...". Report sections stay, marked for the renderer
        to withhold."""
        hidden = self.hide_fields
        sections = []
        for sec in pg["sections"]:
            sec = dict(sec)
            if sec["type"] == "tiles":
                sec["tiles"] = [
                    {**t, "also": [a for a in t["also"] if a["field"] not in hidden]}
                    if t.get("also") else t
                    for t in sec["tiles"] if t.get("field") not in hidden]
                if not sec["tiles"]:
                    continue
            elif sec["type"] == "chart" and sec.get("field") in hidden:
                continue
            elif sec["type"] == "report":
                # Free text from a source: shown to exist, never what it says.
                sections.append({"type": "report", "title": sec.get("title", ""),
                                 "withheld": "not shown in the demo view"})
                continue
            elif "columns" in sec:
                sec["columns"] = [c for c in sec["columns"] if c not in hidden]
                sec["rows"] = [{**r, "fields": {k: v for k, v in r["fields"].items()
                                                if k not in hidden}}
                               for r in sec.get("rows", [])]
            sections.append(sec)
        return {**pg, "sections": sections}

    def tour(self, declared: tuple) -> list:
        """The declared pages this demo shows, in the order `pages:` lists
        their patterns (then declaration order within one pattern)."""
        out: list = []
        for pattern in self.pages:
            out += [p for p in declared
                    if fnmatch.fnmatchcase(p.id, pattern) and p not in out]
        return out

    # ------------------------------------------------------------ redaction --

    def redact(self, text: str) -> str:
        for label, pattern in BUILTIN_REDACTIONS:
            text = pattern.sub(f"[{label} hidden]", text)
        for pattern in self.redact_patterns:
            text = pattern.sub("[hidden]", text)
        for prefix in self.redact_prefixes:
            text = re.sub(r"(?<![\w/])" + re.escape(prefix) + r"[^\s<>\"'\\]*", "[path hidden]", text)
        return text


DISABLED = Demo()


# ------------------------------------------------------------- declaration --

def _strings(raw: Any, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(v, str) and v.strip() for v in raw):
        raise DemoConfigError(f"demo: `{where}` must be a list of non-empty strings")
    return tuple(v.strip() for v in raw)


def _closed(raw: Mapping[str, Any], keys: set[str], where: str) -> None:
    unknown = sorted(set(raw) - keys)
    if unknown:
        raise DemoConfigError(f"{where}: unknown key(s) {', '.join(unknown)}; "
                              f"expected {', '.join(sorted(keys))}")


def parse(raw: Any, page_ids: set[str] | None = None) -> Demo:
    """Parse a `demo:` block. Absent means disabled. `page_ids` is the set of
    declared page ids: a pattern that matches none of them fails the build, so
    a renamed page cannot quietly drop out of the tour."""
    if raw is None:
        return DISABLED
    if not isinstance(raw, Mapping):
        raise DemoConfigError("demo: must be a mapping")
    _closed(raw, _KEYS, "demo")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise DemoConfigError("demo: `enabled` must be true or false")
    pages = _strings(raw.get("pages"), "pages")
    if enabled and not pages:
        raise DemoConfigError("demo: `pages` must name at least one page when enabled")
    if page_ids is not None:
        for pattern in pages:
            if not any(fnmatch.fnmatchcase(pid, pattern) for pid in page_ids):
                raise DemoConfigError(f"demo: `pages` entry {pattern!r} matches no declared page")
    hide = raw.get("hide") or {}
    if not isinstance(hide, Mapping):
        raise DemoConfigError("demo: `hide` must be a mapping")
    _closed(hide, _HIDE_KEYS, "demo.hide")
    facets = hide.get("facets") or {}
    if not isinstance(facets, Mapping):
        raise DemoConfigError("demo: `hide.facets` must map a facet name to a list of values")
    hide_facets = tuple(
        (str(name), frozenset(_strings(values, f"hide.facets.{name}")))
        for name, values in sorted(facets.items()))
    redact = raw.get("redact") or {}
    if not isinstance(redact, Mapping):
        raise DemoConfigError("demo: `redact` must be a mapping")
    _closed(redact, _REDACT_KEYS, "demo.redact")
    patterns = []
    for p in _strings(redact.get("patterns"), "redact.patterns"):
        try:
            patterns.append(re.compile(p))
        except re.error as exc:
            raise DemoConfigError(f"demo: `redact.patterns` entry {p!r} is not a regex: {exc}") from exc
    intro = raw.get("intro")
    if intro is not None and (not isinstance(intro, str) or not intro.strip()):
        raise DemoConfigError("demo: `intro` must be non-empty text")
    title = raw.get("title", "Guided tour")
    if not isinstance(title, str) or not title.strip():
        raise DemoConfigError("demo: `title` must be non-empty text")
    return Demo(
        enabled=enabled, title=title.strip(),
        intro=intro.strip() if isinstance(intro, str) else None, pages=pages,
        hide_kinds=frozenset(_strings(hide.get("kinds"), "hide.kinds")),
        hide_facets=hide_facets,
        hide_fields=frozenset(_strings(hide.get("fields"), "hide.fields")),
        redact_prefixes=_strings(redact.get("prefixes"), "redact.prefixes"),
        redact_patterns=tuple(patterns),
    )


def attach(index: Any, demo: Demo) -> None:
    setattr(index, _ATTR, demo)


def declared(index: Any) -> Demo:
    return getattr(index, _ATTR, DISABLED)


# ------------------------------------------------------------ the index view --

class DemoIndex:
    """The built index as an audience sees it: hidden rows do not exist and
    hidden fields are stripped from the rest. Everything else — the declared
    pages, citations, build info — is the real index's, read through."""

    def __init__(self, index: Any, demo: Demo):
        self._index = index
        self._demo = demo
        self._rows: dict[str, Any] = {}
        for ent in index.all():
            if not demo.row_hidden(ent):
                self._rows[ent.id] = _strip(ent, demo.hide_fields)

    def all(self) -> list:
        return list(self._rows.values())

    def entity(self, entity_id: str) -> Any:
        return self._rows.get(entity_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._index, name)


def _strip(ent: Any, fields: frozenset[str]) -> Any:
    raw = ent.detail.get("fields") if isinstance(ent.detail, Mapping) else None
    if not fields or not isinstance(raw, Mapping) or not (fields & set(raw)):
        return ent
    kept = {k: v for k, v in raw.items() if k not in fields}
    return dataclasses.replace(ent, detail={**ent.detail, "fields": kept})


def view(index: Any) -> DemoIndex:
    """The demo view of one built index, computed once per index (an index
    is immutable once served, §5.9, so the view is too)."""
    cached = getattr(index, _VIEW_ATTR, None)
    if cached is None:
        cached = DemoIndex(index, declared(index))
        setattr(index, _VIEW_ATTR, cached)
    return cached


# ------------------------------------------------------------ the payloads --

def resolved(demo: Demo, index: Any, req: Any) -> bool:
    """Whether a resolved request is one the demo serves and that exists."""
    from .index import pages

    if req is None:
        return False
    if req.view == "landing":
        return True
    return (req.view == "page" and demo.page_allowed(req.page_id)
            and pages.page(index, req.page_id) is not None)


def payload(index: Any, demo: Demo, req: Any, schema_version: int) -> dict[str, Any]:
    """The JSON representation of a demo view: the same evaluation the HTML
    renders, with internal URLs outside the demo nulled and every string
    redacted. Built as a structure and redacted value by value, so a
    redaction can never make the document invalid."""
    from .index import pages

    tour = [{"id": p.id, "title": p.title, "question": p.question, "url": p.url}
            for p in demo.tour(pages.declared(index))]
    doc: dict[str, Any] = {
        "schema_version": schema_version, "demo": True,
        "view": "page" if req.view == "page" else "tour",
        "title": demo.title, "intro": demo.intro, "pages": tour,
        "index": {"built_at": index.build_info.built_at or None},
    }
    if req.view == "page":
        found = pages.page(index, req.page_id)
        doc["page"] = demo.page_doc(pages.evaluate(view(index), found, dict(req.params)))
    return _scrub(doc, demo)


def _scrub(value: Any, demo: Demo, key: str | None = None) -> Any:
    if isinstance(value, Mapping):
        return {k: _scrub(v, demo, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, demo) for v in value]
    if isinstance(value, str):
        if key == "url" and value.startswith("/"):
            return PREFIX + value if internal_allowed(demo, value) else None
        return demo.redact(value)
    if isinstance(value, int) and not isinstance(value, bool):
        # A JSON number can carry a hidden class too (a 12-digit account id).
        text = str(value)
        redacted = demo.redact(text)
        return value if redacted == text else redacted
    return value


# -------------------------------------------------------- the served bytes --

_HREF = re.compile(r'(\s(?:href|action))="(/[^"]*)"')
_ANCHOR_START = re.compile(r"<a\s")
# An href that leaves the console: a scheme (https:, mailto:) or a fragment.
# Everything else is internal — absolute (`/run`) or RELATIVE (`../../run`,
# which the browser resolves against /demo/page/<id> to an operator route).
_EXTERNAL_HREF = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|#)")


def internal_allowed(demo: Demo, url: str) -> bool:
    """Whether an internal URL (as rendered, before the prefix) is a demo
    route: the tour index or a page in the declared set."""
    path = _html.unescape(url).split("?", 1)[0].split("#", 1)[0]
    if path in ("", "/"):
        return True
    segments = [s for s in path.split("/") if s]
    return len(segments) == 2 and segments[0] == "page" and demo.page_allowed(segments[1])


def _link_kept(demo: Demo, href: str) -> bool:
    if _EXTERNAL_HREF.match(_html.unescape(href).strip()):
        return True
    return href.startswith("/") and not href.startswith("//") and internal_allowed(demo, href)


def _anchors(body: str):
    """Each ``<a ... href="X" ...>text</a>`` as (start, end, href, text).

    A scan with str.find, not one regex: the body is rendered from declared and
    adapter text, and a backtracking anchor pattern is polynomial on a body of
    repeated unclosed ``<a `` (CodeQL py/polynomial-redos). Linear in the body."""
    cursor = 0
    for m in _ANCHOR_START.finditer(body):
        start = m.start()
        if start < cursor:
            continue
        tag_end = body.find(">", start)
        if tag_end < 0:
            return
        href_at = body.find('href="', start, tag_end)
        href_end = body.find('"', href_at + 6, tag_end) if href_at >= 0 else -1
        if href_end < 0:
            # Every later `<a ` before tag_end shares this tag_end and finds
            # no complete href either; skipping them keeps the scan linear.
            cursor = tag_end
            continue
        close = body.find("</a>", tag_end)
        if close < 0:
            return
        yield start, close + 4, body[href_at + 6:href_end], body[tag_end + 1:close]
        cursor = close + 4


def _unlink_anchors(body: str, demo: Demo) -> str:
    out: list[str] = []
    last = 0
    for start, end, href, text in _anchors(body):
        if _link_kept(demo, href):
            continue
        out.append(body[last:start])
        out.append(f'<span class="demo-unlinked">{text}</span>')
        last = end
    out.append(body[last:])
    return "".join(out)


def finish(body: str, demo: Demo) -> str:
    """The last step of every demo response: links outside the demo set
    become plain text, links inside it gain the prefix, then the bytes are
    redacted. Order matters — an unwrapped link's URL is gone before the
    redaction runs, so it is never shown even in a form redaction misses."""
    body = _unlink_anchors(body, demo)
    body = _HREF.sub(lambda m: f'{m.group(1)}="{PREFIX}{m.group(2)}"'
                     if m.group(2) != "/styles.css" else m.group(0), body)
    return demo.redact(body)
