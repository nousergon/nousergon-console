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
class Feed:
    id: str
    title: str
    facets: tuple[tuple[str, str], ...]
    kind: str | None = None
    recent_days: int = DEFAULT_RECENT_DAYS
    #: Where the reports are kept, shown on the page as declared.
    archive: str | None = None

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


def parse(raw: Any) -> tuple[Feed, ...]:
    """Validate and freeze the `reports:` block. Raises on anything it could
    not evaluate honestly later."""
    if raw is None:
        return EMPTY
    if not isinstance(raw, Mapping):
        raise ReportsConfigError(
            f"`reports:` must be a mapping with `feeds:`, not {type(raw).__name__}")
    from ..model.kinds import Kind

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
        facets = f.get("facets")
        if not isinstance(facets, Mapping) or not facets:
            # A selector with no facet would claim every row in the index.
            raise ReportsConfigError(
                f"{where}: `facets` must name at least one facet the report rows carry")
        kind = str(f["kind"]).strip() if f.get("kind") else None
        if kind is not None:
            try:
                Kind(kind)
            except ValueError:
                raise ReportsConfigError(f"{where}: unknown `kind` {kind!r}") from None
        days = f.get("recent_days", DEFAULT_RECENT_DAYS)
        if (isinstance(days, bool) or not isinstance(days, int)
                or not 1 <= days <= MAX_RECENT_DAYS):
            raise ReportsConfigError(
                f"{where}: `recent_days` must be an integer 1..{MAX_RECENT_DAYS}")
        feeds.append(Feed(
            id=fid, title=_text(f, "title", where),
            facets=tuple((str(k), str(v)) for k, v in facets.items()),
            kind=kind, recent_days=days,
            archive=(str(f["archive"]).strip() if f.get("archive") else None),
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


def _matches(ent: Any, f: Feed) -> bool:
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


def evaluate(index: Any, f: Feed, now: datetime | None = None) -> dict[str, Any]:
    """One feed over the built index: the newest report (with its text), the
    rest of the recent window (with text), and every older report (dated
    links). Pure: the same index, feed and instant give the same answer."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=f.recent_days)
    rows = []
    for ent in index.all():
        if _matches(ent, f):
            rows.append((_instant(ent.provenance.as_of), ent))
    # Newest first; an undatable row after every dated one, and equal instants
    # break on the id descending, so dated ids list their newest day first.
    floor = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda r: (r[0] is not None, r[0] or floor, r[1].id), reverse=True)
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
        "matched": len(rows), "latest": latest, "recent": recent, "older": older,
    }
