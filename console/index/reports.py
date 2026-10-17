"""Report feeds — "what did the latest report say, what did the ones before it
say, and where are the older ones kept?"

No other pane answers that. A list view (`/run?pane=...`) answers *which rows
exist and what state are they in*: one line per report, and the text one click
away on each row's own page. A reader who wants to READ a daily report — today's,
and the run of days before it — had to open each one in turn. A feed is the
reading view: the newest report rendered in full at the top, the rest of the
recent window below it (collapsed), and every older report listed by date.

**Nothing about any particular report lives in this repo.** A feed is a
`reports:` config declaration: an id, a title, and a SELECTOR over rows other
adapters already mint — every entity whose facets equal the declared `facets`
(and whose kind is `kind`, when declared). The rows carry their own text
(`detail["document"]`, e.g. s3-records `body: markdown`), so the feed reads no
source of its own and is a pure function of the built index, recomputed per
query and never cached (§5.6) — exactly like the planner's alert selectors.
A new report therefore appears on the next index rebuild, with no deploy, no
restart and no edit: the adapter lists its prefix on every pass.

**Retention is the source's, not the console's.** The console persists
nothing (§5.6), so a feed never deletes or moves a report. `recent_days`
decides only how a report is SHOWN: inside the window it is rendered on the
page; outside it, it is a dated link to its own entity page, which renders it
on demand. `archive` is a declared line naming where the reports are kept, so
a reader knows the page is a window over a store, not the store.

**A lead block puts one section first.** A feed may declare `lead:`: a title,
a heading pattern, and the selectors whose NEWEST row it reads (default: the
feed's own). For each, the section under the first markdown heading matching
the pattern is cut out of the row's carried text and shown above the latest
report, newest source first — so a reader meets, say, every "Merged by topic"
decision before scrolling through the rest. The report itself is never edited:
the section is shown twice, once on its own and once in place. A source with no
row, no readable text, or no matching heading says which.

**Absence renders as itself.** A selector that matches no row says so, naming
the selector; a row whose text could not be carried says that, with a link to
its row — never an empty article.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from ..model.entity import DOCUMENT_DETAIL, LABEL_DETAIL

#: The question the reports panes answer, rendered on them (§4.4) and held
#: here so the pane registry, the HTML heading and the JSON cannot drift.
REPORTS_QUESTION = (
    "what did the latest report say, what did the ones before it say, and "
    "where are the older ones kept"
)

#: How many days of reports a feed renders inline when it declares no
#: `recent_days`. Two weeks: long enough to read back across a weekend and a
#: holiday, short enough that the page stays one scroll.
DEFAULT_RECENT_DAYS = 14
MAX_RECENT_DAYS = 90

_ATTR = "_console_report_feeds"
_FEED_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class ReportsConfigError(ValueError):
    """A `reports:` declaration this build cannot evaluate. Raised by `parse`
    during `config.build_index`, so it fails the build naming the feed rather
    than rendering a page that silently selects nothing."""


@dataclass(frozen=True)
class Selector:
    """A kind-and-facets match over built rows, as a lead source names it."""
    facets: tuple[tuple[str, str], ...]
    kind: str | None = None

    @property
    def selector(self) -> str:
        return " ".join(([f"kind={self.kind}"] if self.kind else [])
                        + [f"{k}={v}" for k, v in self.facets])


@dataclass(frozen=True)
class Lead:
    """`lead:` — one section of each source's newest report, shown first."""
    title: str
    #: Searched (case-insensitive) in each ATX heading's text.
    heading: re.Pattern
    #: Whose newest row to read; empty means the feed's own selector.
    sources: tuple[Selector, ...] = ()


@dataclass(frozen=True)
class Feed:
    id: str
    title: str
    facets: tuple[tuple[str, str], ...]
    kind: str | None = None
    recent_days: int = DEFAULT_RECENT_DAYS
    #: Where the reports are kept, shown on the page as declared.
    archive: str | None = None
    lead: Lead | None = None

    @property
    def selector(self) -> str:
        return " ".join(([f"kind={self.kind}"] if self.kind else [])
                        + [f"{k}={v}" for k, v in self.facets])

    @property
    def url(self) -> str:
        return f"/reports/{self.id}"


EMPTY: tuple[Feed, ...] = ()


# ------------------------------------------------------------- declaration --

def _text(entry: Mapping[str, Any], key: str, where: str) -> str:
    value = str(entry.get(key) or "").strip()
    if not value:
        raise ReportsConfigError(f"{where}: `{key}` is required")
    return value


def _kind(entry: Mapping[str, Any], where: str) -> str | None:
    from ..model.kinds import Kind

    kind = str(entry["kind"]).strip() if entry.get("kind") else None
    if kind is not None:
        try:
            Kind(kind)
        except ValueError:
            raise ReportsConfigError(f"{where}: unknown `kind` {kind!r}") from None
    return kind


def _facets(entry: Mapping[str, Any], where: str) -> tuple[tuple[str, str], ...]:
    facets = entry.get("facets")
    if not isinstance(facets, Mapping) or not facets:
        # A selector with no facet would claim every row in the index.
        raise ReportsConfigError(
            f"{where}: `facets` must name at least one facet the report rows carry")
    return tuple((str(k), str(v)) for k, v in facets.items())


def _lead(raw: Any, where: str) -> Lead | None:
    if raw is None:
        return None
    where = f"{where} lead"
    if not isinstance(raw, Mapping):
        raise ReportsConfigError(f"{where}: must be a mapping")
    pattern = _text(raw, "heading", where)
    try:
        heading = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ReportsConfigError(f"{where}: `heading` is not a regex ({exc})") from None
    sources = raw.get("sources") or []
    if not isinstance(sources, list):
        raise ReportsConfigError(f"{where}: `sources` must be a list of selectors")
    parsed = []
    for ordinal, src in enumerate(sources, start=1):
        at = f"{where} source #{ordinal}"
        if not isinstance(src, Mapping):
            raise ReportsConfigError(f"{at}: must be a mapping")
        parsed.append(Selector(facets=_facets(src, at), kind=_kind(src, at)))
    return Lead(title=_text(raw, "title", where), heading=heading, sources=tuple(parsed))


def parse(raw: Any) -> tuple[Feed, ...]:
    """Validate and freeze the `reports:` block. Raises on anything it could
    not evaluate honestly later."""
    if raw is None:
        return EMPTY
    if not isinstance(raw, Mapping):
        raise ReportsConfigError(
            f"`reports:` must be a mapping with `feeds:`, not {type(raw).__name__}")
    feeds: list[Feed] = []
    ids: set[str] = set()
    for ordinal, f in enumerate(raw.get("feeds") or [], start=1):
        where = f"reports feed #{ordinal}"
        if not isinstance(f, Mapping):
            raise ReportsConfigError(f"{where}: must be a mapping")
        fid = _text(f, "id", where)
        where = f"reports feed {fid!r}"
        if not _FEED_ID.match(fid):
            # The id is a URL segment (`/reports/<id>`).
            raise ReportsConfigError(
                f"{where}: `id` must be lowercase letters, digits and dashes")
        if fid in ids:
            raise ReportsConfigError(f"{where}: declared twice")
        ids.add(fid)
        facets = _facets(f, where)
        kind = _kind(f, where)
        days = f.get("recent_days", DEFAULT_RECENT_DAYS)
        if (isinstance(days, bool) or not isinstance(days, int)
                or not 1 <= days <= MAX_RECENT_DAYS):
            raise ReportsConfigError(
                f"{where}: `recent_days` must be an integer 1..{MAX_RECENT_DAYS}")
        feeds.append(Feed(
            id=fid, title=_text(f, "title", where),
            facets=facets, kind=kind, recent_days=days,
            archive=(str(f["archive"]).strip() if f.get("archive") else None),
            lead=_lead(f.get("lead"), where),
        ))
    return tuple(feeds)


def attach(index: Any, feeds: tuple[Feed, ...]) -> None:
    """Hang the parsed feeds off a built index — configuration, not graph, for
    the same reason `planner.attach` gives."""
    setattr(index, _ATTR, feeds)


def declared(index: Any) -> tuple[Feed, ...]:
    return getattr(index, _ATTR, EMPTY)


def feed(index: Any, feed_id: str | None) -> Feed | None:
    """The named feed, or the first declared one when `feed_id` is None."""
    feeds = declared(index)
    if feed_id is None:
        return feeds[0] if feeds else None
    return next((f for f in feeds if f.id == feed_id), None)


# ------------------------------------------------------------- evaluation --

def _instant(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _matches(ent: Any, f: Feed | Selector) -> bool:
    if f.kind and ent.kind.value != f.kind:
        return False
    facets = ent.facets or {}
    return all(facets.get(k) == v for k, v in f.facets)


def _row(ent: Any, when: datetime | None, with_document: bool) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": ent.id,
        "label": str(ent.detail.get(LABEL_DETAIL) or ent.id),
        "at": when.strftime("%Y-%m-%dT%H:%M:%SZ") if when else None,
        "url": ent.route,
        "evidence": ent.provenance.evidence,
        "state": ent.state_value,
    }
    if with_document:
        doc = ent.detail.get(DOCUMENT_DETAIL)
        row["document"] = (dict(doc) if isinstance(doc, Mapping)
                           and isinstance(doc.get("text"), str) else None)
        source = ent.detail.get("document_source")
        if row["document"] is None and isinstance(source, Mapping):
            row["document_unavailable"] = str(source.get("condition") or "unreadable")
    return row


_FLOOR = datetime.min.replace(tzinfo=timezone.utc)
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _newest_first(index: Any, sel: Feed | Selector) -> list[tuple[datetime | None, Any]]:
    rows = [(_instant(ent.provenance.as_of), ent)
            for ent in index.all() if _matches(ent, sel)]
    # Newest first; an undatable row after every dated one, and equal instants
    # break on the id descending, so dated ids list their newest day first.
    rows.sort(key=lambda r: (r[0] is not None, r[0] or _FLOOR, r[1].id), reverse=True)
    return rows


def section(text: str, heading: re.Pattern) -> str | None:
    """The markdown section under the first ATX heading whose text matches
    `heading`: that heading line through the line before the next heading of
    the same or a higher level (or the end). Headings inside fenced code are
    not headings. None when no heading matches."""
    lines = text.splitlines()
    fence: str | None = None
    start = level = None
    for i, line in enumerate(lines):
        mark = _FENCE.match(line)
        if fence is not None:
            if mark and mark.group(1)[0] == fence[0] and len(mark.group(1)) >= len(fence):
                fence = None
            continue
        if mark:
            fence = mark.group(1)
            continue
        h = _ATX.match(line)
        if not h:
            continue
        depth = len(h.group(1))
        if start is not None:
            if depth <= level:
                return "\n".join(lines[start:i]).strip()
        elif heading.search(h.group(2) or ""):
            start, level = i, depth
    return "\n".join(lines[start:]).strip() if start is not None else None


def _lead_item(index: Any, lead: Lead, sel: Feed | Selector) -> dict[str, Any]:
    rows = _newest_first(index, sel)
    if not rows:
        return {"selector": sel.selector, "id": None, "at": None,
                "section": None, "section_unavailable": "no row matches the selector"}
    when, ent = rows[0]
    item = {"selector": sel.selector, **_row(ent, when, True)}
    doc = item.pop("document")
    item.pop("document_unavailable", None)
    if doc is None:
        item["section"] = None
        item["section_unavailable"] = "its text is not carried or could not be read"
        return item
    text = section(doc["text"], lead.heading)
    if text is None:
        item["section"] = None
        item["section_unavailable"] = (
            f"no heading matches {lead.heading.pattern!r}"
            + (" in the part carried (the document was cut)" if doc.get("truncated") else ""))
        return item
    item["section"] = {"text": text, "format": doc.get("format")}
    return item


def _lead_block(index: Any, f: Feed) -> dict[str, Any] | None:
    if f.lead is None:
        return None
    items = [_lead_item(index, f.lead, sel) for sel in (f.lead.sources or (f,))]
    # Newest source first, like the feed; a source with no dated row last.
    items.sort(key=lambda r: (r["at"] is not None, r["at"] or ""), reverse=True)
    return {"title": f.lead.title, "heading": f.lead.heading.pattern, "items": items}


def evaluate(index: Any, f: Feed, now: datetime | None = None) -> dict[str, Any]:
    """One feed over the built index: the lead block (when declared), the
    newest report (with its text), the rest of the recent window (with text),
    and every older report (dated links). Pure: the same index, feed and
    instant give the same answer."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=f.recent_days)
    rows = _newest_first(index, f)
    latest = _row(rows[0][1], rows[0][0], True) if rows else None
    recent, older = [], []
    for when, ent in rows[1:]:
        if when is not None and when >= cutoff:
            recent.append(_row(ent, when, True))
        else:
            older.append(_row(ent, when, False))
    return {
        "id": f.id, "title": f.title, "url": f.url, "selector": f.selector,
        "recent_days": f.recent_days, "archive": f.archive,
        "matched": len(rows), "lead": _lead_block(index, f),
        "latest": latest, "recent": recent, "older": older,
    }
