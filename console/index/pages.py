"""Declared pages — "what does this part of the system look like right now?"

A list view (`/run?pane=cost`) answers *which rows exist and what state they
are in*, one row per line. A page answers the question a reader actually brings
to a part of the system — "is spend on track", "how is the portfolio doing" —
by laying out sections over rows other adapters already mint: headline tiles,
a table, a chart, the latest report, and what is wrong. That is the layer the
console was missing (plan `nous-ergon-ops/plans/2026-10-28-console-replaces-
dashboard.md` §2, alpha-engine-config-I12783).

**Nothing about any particular page lives in this repo.** A page is a `pages:`
config declaration — an id, a title, the question it answers, and sections,
each a SELECTOR over built rows (kind and facets, as a report feed selects) and
the declared field names to show. It reads no source of its own, so it is a
pure function of the built index, recomputed per request and never cached
(§5.6). Adding, changing or removing a page is a config fragment, with no
release of this code.

**The section vocabulary is closed** — `tiles`, `table`, `chart`, `report`,
`exceptions` — for the reason §5.8's render hints are: an open set is a plugin
API, and a plugin API is a per-page rendering path with a nicer name.

**The URL is the whole state** (§3.2). `?range=7d` narrows every table, chart
and exception list to rows (and points) inside the window; each declared
variable is a facet name, and `?<facet>=<value>` narrows every section whose
rows carry that facet. A pasted URL reproduces the page on a cold load.

**A tile always shows its staleness and its baseline** (`products/console.md`
§ Navigation: a tile without either is forbidden). An undeclared baseline says
"no baseline", never nothing; a selector matching no row says so, naming it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from ..model.entity import LABEL_DETAIL
from ..model.fields import Render, format_value, parse as parse_fields, series_points

_ATTR = "_console_pages"
_PAGE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: The closed section vocabulary. Adding one is a PR here, never config.
SECTION_TYPES = ("tiles", "table", "chart", "report", "exceptions")

#: The closed time-range vocabulary, as the URL spells it.
RANGES: dict[str, timedelta | None] = {
    "24h": timedelta(hours=24), "7d": timedelta(days=7),
    "30d": timedelta(days=30), "90d": timedelta(days=90),
    "1y": timedelta(days=365), "all": None,
}
RANGE_PARAM = "range"
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

#: Priority order for an exceptions section: P0 first, then rows nobody has
#: triaged yet, then P1-P3 (Brian, 2026-10-28 18:13Z).
_PRIORITY = re.compile(r"^(?:P([0-3])|SEV([1-4]))$", re.IGNORECASE)


class PagesConfigError(ValueError):
    """A `pages:` declaration this build cannot evaluate. Raised by `parse`
    during `config.build_index`, so it fails the build naming the page and
    section rather than rendering a page that silently selects nothing."""


@dataclass(frozen=True)
class Selector:
    facets: tuple[tuple[str, str], ...]
    kind: str | None = None

    @property
    def text(self) -> str:
        return " ".join(([f"kind={self.kind}"] if self.kind else [])
                        + [f"{k}={v}" for k, v in self.facets])


@dataclass(frozen=True)
class Tile:
    label: str
    select: Selector
    field: str


@dataclass(frozen=True)
class Section:
    type: str
    title: str
    select: Selector | None = None
    #: tiles only.
    tiles: tuple[Tile, ...] = ()
    #: table: the declared fields shown as columns (empty: every declared
    #: field, in declaration order). chart: the one timeseries field.
    fields: tuple[str, ...] = ()
    #: table: a declared field to order by, `-` prefix for descending.
    sort: str | None = None
    limit: int = DEFAULT_LIMIT
    #: report only: the `reports:` feed id.
    feed: str | None = None


@dataclass(frozen=True)
class Page:
    id: str
    title: str
    question: str
    sections: tuple[Section, ...]
    range: str = "all"
    variables: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        return f"/page/{self.id}"


EMPTY: tuple[Page, ...] = ()


# ------------------------------------------------------------- declaration --

def _text(entry: Mapping[str, Any], key: str, where: str) -> str:
    value = str(entry.get(key) or "").strip()
    if not value:
        raise PagesConfigError(f"{where}: `{key}` is required")
    return value


def _selector(raw: Any, where: str) -> Selector:
    from ..model.kinds import Kind

    if not isinstance(raw, Mapping):
        raise PagesConfigError(f"{where}: `select` must be a mapping with `facets`")
    facets = raw.get("facets")
    if not isinstance(facets, Mapping) or not facets:
        # A selector with no facet would claim every row in the index.
        raise PagesConfigError(
            f"{where}: `select.facets` must name at least one facet the rows carry")
    kind = str(raw["kind"]).strip() if raw.get("kind") else None
    if kind is not None:
        try:
            Kind(kind)
        except ValueError:
            raise PagesConfigError(f"{where}: unknown `kind` {kind!r}") from None
    return Selector(facets=tuple((str(k), str(v)) for k, v in facets.items()), kind=kind)


def _names(raw: Any, where: str, key: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(n, str) and n.strip() for n in raw):
        raise PagesConfigError(f"{where}: `{key}` must be a list of names")
    return tuple(n.strip() for n in raw)


def _section(raw: Any, where: str, feeds: set[str] | None) -> Section:
    if not isinstance(raw, Mapping):
        raise PagesConfigError(f"{where}: must be a mapping")
    kind = str(raw.get("type") or "").strip()
    if kind not in SECTION_TYPES:
        raise PagesConfigError(
            f"{where}: `type` must be one of {', '.join(SECTION_TYPES)}, not {kind!r}")
    where = f"{where} ({kind})"
    title = str(raw.get("title") or "").strip()
    if kind == "tiles":
        tiles = raw.get("tiles")
        if not isinstance(tiles, list) or not tiles:
            raise PagesConfigError(f"{where}: `tiles` must be a non-empty list")
        parsed = []
        for n, t in enumerate(tiles, start=1):
            at = f"{where} tile #{n}"
            if not isinstance(t, Mapping):
                raise PagesConfigError(f"{at}: must be a mapping")
            parsed.append(Tile(label=_text(t, "label", at),
                               select=_selector(t.get("select"), at),
                               field=_text(t, "field", at)))
        return Section(type=kind, title=title, tiles=tuple(parsed))
    if kind == "report":
        feed = _text(raw, "feed", where)
        if feeds is not None and feed not in feeds:
            raise PagesConfigError(f"{where}: no `reports:` feed {feed!r} is declared")
        return Section(type=kind, title=title, feed=feed)
    if not title:
        raise PagesConfigError(f"{where}: `title` is required")
    select = _selector(raw.get("select"), where)
    fields = _names(raw.get("fields"), where, "fields")
    if kind == "chart" and len(fields) != 1:
        raise PagesConfigError(f"{where}: `fields` must name exactly one timeseries field")
    limit = raw.get("limit", DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise PagesConfigError(f"{where}: `limit` must be an integer 1..{MAX_LIMIT}")
    sort = str(raw["sort"]).strip() if raw.get("sort") else None
    if sort is not None and kind != "table":
        raise PagesConfigError(f"{where}: only a table takes `sort`")
    return Section(type=kind, title=title, select=select, fields=fields,
                   sort=sort, limit=limit)


def parse(raw: Any, feeds: set[str] | None = None) -> tuple[Page, ...]:
    """Validate and freeze the `pages:` block — a mapping of page id to page.

    ``feeds`` is the set of declared report-feed ids; a `report` section naming
    any other fails here, by name. Raises on anything it could not evaluate
    honestly later."""
    if raw is None:
        return EMPTY
    if not isinstance(raw, Mapping):
        raise PagesConfigError(
            f"`pages:` must be a mapping of page id to page, not {type(raw).__name__}")
    pages = []
    for pid, p in raw.items():
        pid = str(pid)
        where = f"page {pid!r}"
        if not _PAGE_ID.match(pid):
            # The id is a URL segment (`/page/<id>`).
            raise PagesConfigError(f"{where}: id must be lowercase letters, digits and dashes")
        if not isinstance(p, Mapping):
            raise PagesConfigError(f"{where}: must be a mapping")
        sections = p.get("sections")
        if not isinstance(sections, list) or not sections:
            raise PagesConfigError(f"{where}: `sections` must be a non-empty list")
        rng = str(p.get("range") or "all")
        if rng not in RANGES:
            raise PagesConfigError(f"{where}: `range` must be one of {', '.join(RANGES)}")
        variables = _names(p.get("variables"), where, "variables")
        if RANGE_PARAM in variables:
            raise PagesConfigError(f"{where}: `{RANGE_PARAM}` is the time range, not a variable")
        pages.append(Page(
            id=pid, title=_text(p, "title", where), question=_text(p, "question", where),
            sections=tuple(_section(s, f"{where} section #{n}", feeds)
                           for n, s in enumerate(sections, start=1)),
            range=rng, variables=variables))
    return tuple(pages)


def attach(index: Any, pages: tuple[Page, ...]) -> None:
    """Hang the parsed pages off a built index — configuration, not graph, for
    the same reason `planner.attach` gives."""
    setattr(index, _ATTR, pages)


def declared(index: Any) -> tuple[Page, ...]:
    return getattr(index, _ATTR, EMPTY)


def page(index: Any, page_id: str | None) -> Page | None:
    return next((p for p in declared(index) if p.id == page_id), None)


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


def _matches(ent: Any, sel: Selector, variables: Mapping[str, str]) -> bool:
    if sel.kind and ent.kind.value != sel.kind:
        return False
    facets = ent.facets or {}
    if not all(facets.get(k) == v for k, v in sel.facets):
        return False
    # A variable narrows only rows that carry its facet: a page-wide
    # `provider=aws` must not empty a section about something else.
    return all(facets.get(k) == v for k, v in variables.items() if k in facets)


def _select(index: Any, sel: Selector, variables: Mapping[str, str]) -> list:
    rows = [e for e in index.all() if _matches(e, sel, variables)]
    floor = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda e: (_instant(e.provenance.as_of) or floor, e.id), reverse=True)
    return rows


def _in_range(ent: Any, since: datetime | None) -> bool:
    if since is None:
        return True
    when = _instant(ent.provenance.as_of)
    # An undated row is not provably inside the window, and hiding it would
    # be a silent drop; it stays, and its as-of cell says it has no stamp.
    return when is None or when >= since


def _fields(ent: Any) -> dict:
    return {f.name: f for f in parse_fields(ent.detail.get("fields"))}


def _declared_order(ent: Any) -> list[str]:
    raw = ent.detail.get("fields")
    return [str(n) for n in raw if str(n) != "question"] if isinstance(raw, Mapping) else []


def _row(ent: Any, columns: tuple[str, ...] | list[str]) -> dict[str, Any]:
    by_name = _fields(ent)
    return {
        "id": ent.id, "url": ent.route, "state": ent.state_value,
        "label": str(ent.detail.get(LABEL_DETAIL) or ent.id),
        "as_of": ent.provenance.as_of, "evidence": ent.provenance.evidence,
        "fields": {c: (by_name[c].value if c in by_name else None) for c in columns},
    }


def _tile(index: Any, tile: Tile, variables: Mapping[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {"label": tile.label, "selector": tile.select.text,
                           "field": tile.field}
    rows = _select(index, tile.select, variables)
    if not rows:
        return {**out, "id": None, "absent": "no row matches the selector"}
    ent = rows[0]
    out.update(id=ent.id, url=ent.route, state=ent.state_value,
               as_of=ent.provenance.as_of, evidence=ent.provenance.evidence)
    f = _fields(ent).get(tile.field)
    if f is None:
        return {**out, "absent": f"the newest row carries no field {tile.field!r}"}
    out.update(value=f.value, text=format_value(f), unit=f.unit,
               baseline={"declared": f.baseline_declared, "value": f.baseline},
               defect=f.defect)
    if f.comparable and isinstance(f.value, (int, float)) and not isinstance(f.value, bool):
        out["versus_baseline"] = ("above" if f.value > f.baseline else
                                  "below" if f.value < f.baseline else "at")
    return out


def _sort_key(sort: str):
    desc = sort.startswith("-")
    name = sort.lstrip("-")

    def key(row: dict) -> tuple:
        v = row["fields"].get(name)
        number = isinstance(v, (int, float)) and not isinstance(v, bool)
        # A row without the value sorts last in either direction.
        present = v is not None
        if desc:
            return (not present, -(float(v)) if number else 0.0, "" if number else str(v))
        return (not present, float(v) if number else 0.0, "" if number else str(v))

    return key


def priority_rank(ent: Any) -> int:
    """P0 first, then untriaged rows, then P1, P2, P3 (SEVn reads as P(n-1)).

    Read from a `priority` facet, else a `priority` field. Anything that is not
    a recognised priority is untriaged — never guessed upward or downward."""
    raw = (ent.facets or {}).get("priority")
    if raw is None:
        f = _fields(ent).get("priority")
        raw = f.value if f is not None else None
    m = _PRIORITY.match(str(raw).strip()) if raw is not None else None
    if not m:
        return 1
    level = int(m.group(1)) if m.group(1) is not None else int(m.group(2)) - 1
    return 0 if level == 0 else level + 1


#: The priority tiers, in display order; `priority_rank` indexes this.
PRIORITY_TIERS = ("P0", "untriaged", "P1", "P2", "P3")

#: The question the exceptions page answers (§4.4).
EXCEPTIONS_QUESTION = "what is wrong, most urgent first"


def by_priority(rows: list) -> list:
    """Rows in exception-list order: P0, untriaged, P1-P3; within a tier the
    worst state first, then the OLDEST first (the longest-open problem of a
    tier is the most overdue)."""
    from ..render.html import state_rank

    ceiling = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(rows, key=lambda e: (priority_rank(e), state_rank(e.state_value),
                                       _instant(e.provenance.as_of) or ceiling, e.id))


def _chart(ent: Any, field: str, since: datetime | None) -> dict[str, Any]:
    f = _fields(ent).get(field)
    if f is None:
        return {"absent": f"the newest row carries no field {field!r}"}
    points = series_points(f.value) if f.render is Render.TIMESERIES else None
    if points is None:
        return {"absent": f"field {field!r} is not a timeseries this console can read"}
    dropped = 0
    if since is not None:
        kept = []
        for label, y in points:
            when = _instant(label)
            if when is not None and len(str(label).strip()) == 10:
                # A bare date is a whole day: it is inside the range when any
                # part of it is.
                when += timedelta(days=1)
            if when is not None and when <= since:
                dropped += 1
                continue
            kept.append((label, y))
        points = kept
    whole = _fields(ent).get(f.of or "")
    return {"points": [[label, y] for label, y in points], "unit": f.unit,
            "outside_range": dropped,
            "reference": ({"name": whole.name, "value": whole.value}
                          if whole is not None else None)}


def _section_doc(index: Any, s: Section, variables: Mapping[str, str],
                 since: datetime | None) -> dict[str, Any]:
    from ..render.html import is_exception, state_rank

    doc: dict[str, Any] = {"type": s.type, "title": s.title}
    if s.type == "tiles":
        doc["tiles"] = [_tile(index, t, variables) for t in s.tiles]
        return doc
    if s.type == "report":
        from . import reports

        feed = reports.feed(index, s.feed)
        doc["feed"] = s.feed
        if feed is None:
            doc["absent"] = f"no report feed {s.feed!r}"
            return doc
        doc.update(title=s.title or feed.title, url=feed.url,
                   latest=reports.evaluate(index, feed)["latest"])
        return doc
    assert s.select is not None
    doc["selector"] = s.select.text
    rows = _select(index, s.select, variables)
    if s.type == "chart":
        doc["field"] = s.fields[0]
        if not rows:
            doc["absent"] = "no row matches the selector"
            return doc
        ent = rows[0]
        doc.update(id=ent.id, url=ent.route, as_of=ent.provenance.as_of,
                   **_chart(ent, s.fields[0], since))
        return doc
    in_range = [e for e in rows if _in_range(e, since)]
    if s.type == "exceptions":
        bad = by_priority([e for e in in_range if is_exception(e)])
        doc.update(matched=len(in_range), columns=[],
                   rows=[_row(e, ()) for e in bad[:s.limit]], total=len(bad))
        return doc
    columns = list(s.fields)
    if not columns:
        seen: dict[str, None] = {}
        for e in in_range:
            for n in _declared_order(e):
                seen.setdefault(n, None)
        columns = list(seen)
    table = [_row(e, columns) for e in in_range]
    if s.sort:
        table.sort(key=_sort_key(s.sort))
    else:
        table.sort(key=lambda r: state_rank(r["state"]))  # stable: newest within a state
    doc.update(columns=columns, matched=len(rows), total=len(table),
               rows=table[:s.limit], outside_range=len(rows) - len(in_range))
    return doc


def _variable_options(index: Any, p: Page) -> dict[str, list[str]]:
    """Each variable's values, from the rows the page's selectors match."""
    selectors = [s.select for s in p.sections if s.select is not None]
    selectors += [t.select for s in p.sections for t in s.tiles]
    options: dict[str, set[str]] = {v: set() for v in p.variables}
    for ent in index.all():
        if not any(_matches(ent, sel, {}) for sel in selectors):
            continue
        for v in p.variables:
            value = (ent.facets or {}).get(v)
            if value is not None:
                options[v].add(str(value))
    return {v: sorted(vals) for v, vals in options.items()}


def evaluate(index: Any, p: Page, params: Mapping[str, str] | None = None,
             now: datetime | None = None) -> dict[str, Any]:
    """One page over the built index, for the URL's range and variables.
    Pure: the same index, page, parameters and instant give the same answer,
    and HTML and JSON both render this one result (§3.8)."""
    params = params or {}
    now = now or datetime.now(timezone.utc)
    rng = params.get(RANGE_PARAM) or p.range
    if rng not in RANGES:
        rng = p.range
    span = RANGES[rng]
    since = now - span if span is not None else None
    variables = {v: params[v] for v in p.variables if params.get(v)}
    options = _variable_options(index, p)
    return {
        "id": p.id, "title": p.title, "url": p.url, "question": p.question,
        "range": {"value": rng, "default": p.range, "options": list(RANGES),
                  "since": since.strftime("%Y-%m-%dT%H:%M:%SZ") if since else None},
        "variables": [{"name": v, "value": variables.get(v), "options": options[v]}
                      for v in p.variables],
        "sections": [_section_doc(index, s, variables, since) for s in p.sections],
    }
