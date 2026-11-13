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
`exceptions`, `text` — for the reason §5.8's render hints are: an open set is a plugin
API, and a plugin API is a per-page rendering path with a nicer name.

**The URL is the whole state** (§3.2). `?range=7d` narrows every table, chart
and exception list to rows (and points) inside the window; each declared
variable is a facet name, and `?<facet>=<value>` narrows every section whose
rows carry that facet. A pasted URL reproduces the page on a cold load.

**A table may be sorted by its reader** (Brian, 2026-11-04: "make this
running list sortable by merge type, repo, and date"). A table that declares
`sortable: [field, ...]` takes `?sort=<field>` or `?sort=-<field>` from the
URL — still the whole state — for any field it lists, and its column header
links to the other direction. A `sort` naming a field the table does not list
is ignored for that table, never an error: one page-wide parameter may suit
one table and not its neighbour. The declared `sort` stays the default.

**A tile always shows its staleness and its baseline** (`products/console.md`
§ Navigation: a tile without either is forbidden). An undeclared baseline says
"no baseline", never nothing; a selector matching no row says so, naming it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence, cast

from ..model.entity import LABEL_DETAIL, Entity
from ..model.fields import Field, Render, format_value, parse as parse_fields, series_points

_ATTR = "_console_pages"
_PAGE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
#: A site-index group name: shown as a heading, so plain words only.
_SECTION = re.compile(r"^[a-z0-9][a-z0-9 -]{0,39}$")

#: The closed section vocabulary. Adding one is a PR here, never config.
SECTION_TYPES = ("tiles", "table", "chart", "grid", "report", "exceptions", "text", "decisions")
#: grid: the most columns (days) one grid shows, and its default.
MAX_GRID_COLUMNS = 60
DEFAULT_GRID_COLUMNS = 14

#: The closed time-range vocabulary, as the URL spells it.
RANGES: dict[str, timedelta | None] = {
    "24h": timedelta(hours=24), "7d": timedelta(days=7),
    "30d": timedelta(days=30), "90d": timedelta(days=90),
    "1y": timedelta(days=365), "all": None,
}
RANGE_PARAM = "range"
SORT_PARAM = "sort"
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
#: The most timeseries one chart overlays (alpha-engine-config-I12798): a
#: return beside its benchmarks. One colour token per series
#: (`--chart-1`..`--chart-6`); more lines than that stop being readable.
MAX_CHART_SERIES = 6

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
    #: Further fields OF THE SAME ROW shown under the value, each as
    #: (field, label): a return beside its benchmarks, a count beside what it
    #: counts. Never a second row, so the tile still states one source and one
    #: as-of (alpha-engine-config-I12783).
    also: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Annotation:
    """One source of event marks on a chart (alpha-engine-config-I12784).

    `select` names the event rows (a deploy, a promotion, an incident) another
    adapter already mints; `at` is the declared field carrying each event's
    date or instant; `text` the declared field naming it (absent: the row's
    label). `label` says what kind of event the mark is, in words, because a
    mark's position alone carries nothing (§5.7)."""
    label: str
    select: Selector
    at: str
    text: str | None = None


@dataclass(frozen=True)
class Choice:
    """One button on a decision card: the value posted and the words shown."""
    value: str
    label: str
    primary: bool = False


@dataclass(frozen=True)
class Act:
    """A `decisions` section's declared write (Brian 2026-11-13: pages are
    read-only by default; a page opts in to acting by declaring this). The
    console posts nothing itself and holds no write grant: the reader's
    browser posts JSON to ``post``, a same-origin path the console does not
    serve, owned by the writer that holds the record's one write grant."""
    post: str
    #: the row field identifying the row to the writer, posted under its own name.
    key: str
    #: further row fields posted under their own names (e.g. the tenant).
    send: tuple[str, ...]
    #: the body field the chosen button's value is posted under.
    choice_field: str
    choices: tuple[Choice, ...]
    #: "change" button: its value, its label, and the selects it opens,
    #: each a body field and its (value, label) options.
    change: Choice | None = None
    change_fields: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    #: whether the card carries a free-text note, posted as `note`.
    note: bool = True
    #: the row field shown as the card's headline.
    headline: str | None = None


@dataclass(frozen=True)
class Section:
    type: str
    title: str
    select: Selector | None = None
    #: tiles only.
    tiles: tuple[Tile, ...] = ()
    #: table: the declared fields shown as columns (empty: every declared
    #: field, in declaration order). chart: the one timeseries field, or 2..
    #: `MAX_CHART_SERIES` timeseries of the same row overlaid as lines.
    fields: tuple[str, ...] = ()
    #: table: a declared field to order by, `-` prefix for descending.
    sort: str | None = None
    limit: int = DEFAULT_LIMIT
    #: report only: the `reports:` feed id.
    feed: str | None = None
    #: table only: the `sort` field's values in display order (a closed set
    #: such as window ids). A value the list does not name sorts after every
    #: listed one, and a row without the field sorts last.
    sort_order: tuple[str, ...] = ()
    #: What the section says when its selector matches no row at all — the
    #: source has published nothing yet ("no data yet: ..."). Rendered beside
    #: the selector, never instead of it.
    empty: str | None = None
    #: text only: the declared prose, rendered as markdown (raw HTML off,
    #: citations linked) — the narrative a guided page reads in, between the
    #: sections that carry its evidence (alpha-engine-config-I13023).
    body: str | None = None
    #: chart only: event marks drawn on the chart's dates and listed under it.
    annotations: tuple[Annotation, ...] = ()
    #: chart only: draw ACROSS the matched rows, one point per row at this
    #: declared field's value, rather than one row's timeseries (Brian
    #: 2026-11-12: the optimizer charts the old dashboard drew per name and
    #: per day). Dates draw as lines in date order; anything else as bars,
    #: in `sort` order, `limit` of them.
    x: str | None = None
    #: grid only: the field naming each ROW of a grid (e.g. ``ticker``), whose
    #: columns are the `x` values and whose cells are the one declared field
    #: (Brian 2026-11-13: the old dashboard's target-weight-by-day grid).
    y: str | None = None
    #: grid only: how many of the newest `x` values to show as columns.
    columns: int = DEFAULT_GRID_COLUMNS
    #: table only: the fields a reader may sort by from the URL (`?sort=`).
    sortable: tuple[str, ...] = ()
    #: table only: a declared `ticks:` list and the row field keying it, so each
    #: row carries a checkbox (`index/ticks.py`), e.g. ("reading-list", "item_id").
    tick: tuple[str, str] | None = None
    #: decisions only: the declared write and the labelled fields each card shows.
    act: Act | None = None
    labels: tuple[tuple[str, str], ...] = ()
    #: table only: a PLAIN table (Brian 2026-11-13: the order book "is so bad,
    #: I cannot read it"): headed by each field's declared `label`, values
    #: written for a reader (12.03%, $45,730, 891), the first column linking
    #: the row, and no id, state, source, as-of or evidence columns. The
    #: provenance stays one click away on the row's own page.
    plain: bool = False
    #: plain tables: fields written with a sign and toned as a gain or a
    #: loss (a trade: +891 bought, -88 sold). The sign carries it; the tone
    #: only adds to it.
    signed: tuple[str, ...] = ()


@dataclass(frozen=True)
class Link:
    title: str
    url: str


@dataclass(frozen=True)
class Page:
    id: str
    title: str
    question: str
    sections: tuple[Section, ...]
    range: str = "all"
    variables: tuple[str, ...] = ()
    #: Variables that open on their NEWEST value rather than on all values
    #: (`default_newest:`), for a page that reads one day at a time: an order
    #: book opened with no day picked would mix every saved day in one table.
    #: The newest value is the last of the sorted options.
    default_newest: tuple[str, ...] = ()
    #: Declared links shown under the question (the source of truth a page's
    #: numbers come from, the definitions behind them).
    links: tuple[Link, ...] = ()
    #: Which group of the site index (`/index`) lists this page, e.g.
    #: `trading`, `models`, `cost`, `archives`. Optional: an undeclared
    #: section lists the page under `other`, never hides it.
    section: str = "other"
    #: Which product the page belongs to (`index/products.py`), e.g.
    #: `crucible`. None: org-wide. The site index groups by product first.
    product: str | None = None

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


#: A facet value that matches any row CARRYING the facet, whatever its value.
ANY = "*"


def _selector(raw: Any, where: str, kind_only: bool = False) -> Selector:
    """A kind-and-facets selector. ``kind_only`` admits a selector naming only
    a kind (a home area may own a whole kind); otherwise at least one facet is
    required, because a selector with no facet would claim every row of every
    kind."""
    from ..model.kinds import Kind

    if not isinstance(raw, Mapping):
        raise PagesConfigError(f"{where}: `select` must be a mapping with `facets`")
    facets = raw.get("facets") or {}
    if not isinstance(facets, Mapping) or (not facets and not (kind_only and raw.get("kind"))):
        raise PagesConfigError(
            f"{where}: `select.facets` must name at least one facet the rows carry"
            + (" (or the selector must name a `kind`)" if kind_only else ""))
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


#: A declared link opens in a browser: http(s), or a path on this console.
_LINK_URL = re.compile(r"^(https?://\S+|/\S*)$")


def _links(raw: Any, where: str) -> tuple[Link, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise PagesConfigError(f"{where}: `links` must be a list of {{title, url}}")
    out = []
    for n, entry in enumerate(raw, start=1):
        at = f"{where} link #{n}"
        if not isinstance(entry, Mapping):
            raise PagesConfigError(f"{at}: must be a mapping with `title` and `url`")
        url = _text(entry, "url", at)
        if not _LINK_URL.match(url):
            raise PagesConfigError(f"{at}: `url` must be http(s):// or a /path, not {url!r}")
        out.append(Link(title=_text(entry, "title", at), url=url))
    return tuple(out)


def _annotations(raw: Any, where: str) -> tuple[Annotation, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise PagesConfigError(
            f"{where}: `annotations` must be a non-empty list of {{label, select, at}}")
    out = []
    for n, entry in enumerate(raw, start=1):
        at = f"{where} annotation #{n}"
        if not isinstance(entry, Mapping):
            raise PagesConfigError(f"{at}: must be a mapping with `label`, `select` and `at`")
        text = str(entry.get("text") or "").strip() or None
        out.append(Annotation(label=_text(entry, "label", at),
                              select=_selector(entry.get("select"), at),
                              at=_text(entry, "at", at), text=text))
    return tuple(out)


def _empty(raw: Mapping[str, Any], where: str) -> str | None:
    if "empty" not in raw:
        return None
    text = str(raw.get("empty") or "").strip()
    if not text:
        raise PagesConfigError(f"{where}: `empty` must be non-blank text when declared")
    return text


def _tick(raw: Any, where: str, kind: str, ticks: set[str] | None) -> tuple[str, str] | None:
    if raw is None:
        return None
    if kind != "table":
        raise PagesConfigError(f"{where}: only a table takes `tick`")
    if not isinstance(raw, Mapping) or set(raw) != {"list", "key"}:
        raise PagesConfigError(f"{where}: `tick` must be {{list: <ticks name>, key: <field>}}")
    name, key = str(raw["list"]).strip(), str(raw["key"]).strip()
    if ticks is not None and name not in ticks:
        raise PagesConfigError(f"{where}: no `ticks:` list {name!r} is declared")
    if not key:
        raise PagesConfigError(f"{where}: `tick.key` must name a field")
    return name, key


def _plain_fields(raw: object, where: str
                  ) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], tuple[str, ...]]:
    """A plain table's `fields`: each a field name, or ``{field, label,
    signed}``. A bare name is headed by itself with underscores as spaces."""
    if not isinstance(raw, list) or not raw:
        raise PagesConfigError(f"{where}: a plain table lists its `fields`")
    names: list[str] = []
    labels: list[tuple[str, str]] = []
    signed: list[str] = []
    for n, entry in enumerate(cast("list[object]", raw), start=1):
        if isinstance(entry, str) and entry.strip():
            name, label, sign = entry.strip(), entry.strip().replace("_", " "), False
        elif isinstance(entry, Mapping):
            m = cast("Mapping[str, object]", entry)
            name = _text(m, "field", f"{where} field #{n}")
            label = str(m.get("label") or name.replace("_", " ")).strip()
            sign = m.get("signed", False)
            if not isinstance(sign, bool):
                raise PagesConfigError(f"{where} field #{n}: `signed` is true or false")
            stray = sorted(set(m) - {"field", "label", "signed"})
            if stray:
                raise PagesConfigError(f"{where} field #{n}: unknown key(s) {', '.join(stray)}")
        else:
            raise PagesConfigError(f"{where} field #{n}: a field name or `{{field, label}}`")
        if name in names:
            raise PagesConfigError(f"{where}: `fields` names {name!r} twice")
        names.append(name)
        labels.append((name, label))
        if sign:
            signed.append(name)
    return tuple(names), tuple(labels), tuple(signed)


def _section(raw: Any, where: str, feeds: set[str] | None,
             ticks: set[str] | None = None) -> Section:
    if not isinstance(raw, Mapping):
        raise PagesConfigError(f"{where}: must be a mapping")
    kind = str(raw.get("type") or "").strip()
    if kind not in SECTION_TYPES:
        raise PagesConfigError(
            f"{where}: `type` must be one of {', '.join(SECTION_TYPES)}, not {kind!r}")
    where = f"{where} ({kind})"
    title = str(raw.get("title") or "").strip()
    empty = _empty(raw, where)
    if empty is not None and kind == "report":
        raise PagesConfigError(f"{where}: a report section already says \"no report yet\"; "
                               "`empty` is for selector sections")
    if empty is not None and kind == "text":
        raise PagesConfigError(f"{where}: `empty` is for selector sections; a text "
                               "section selects no rows")
    if kind in ("text", "tiles", "report"):
        # These return before the table/chart keys are read, so a key only a
        # table or chart takes would otherwise be accepted and ignored
        # (codex aa70f0fe8eaa1d58).
        stray = [k for k in ("sortable", "sort", "sort_order", "annotations") if k in raw]
        if stray:
            raise PagesConfigError(
                f"{where}: {', '.join(f'`{k}`' for k in stray)} applies to a table or chart, "
                f"not a {kind} section")
    if kind == "text":
        body = raw.get("body")
        if not isinstance(body, str) or not body.strip():
            raise PagesConfigError(f"{where}: `body` must be non-empty text")
        return Section(type=kind, title=title, body=body.strip())
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
                               field=_text(t, "field", at),
                               also=_also(t.get("also"), at)))
        return Section(type=kind, title=title, tiles=tuple(parsed), empty=empty)
    if kind == "report":
        feed = _text(raw, "feed", where)
        if feeds is not None and feed not in feeds:
            raise PagesConfigError(f"{where}: no `reports:` feed {feed!r} is declared")
        return Section(type=kind, title=title, feed=feed)
    if not title:
        raise PagesConfigError(f"{where}: `title` is required")
    select = _selector(raw.get("select"), where)
    if kind == "decisions":
        return _decisions(cast("Mapping[str, object]", raw), where, title, select, empty)
    if "act" in raw:
        raise PagesConfigError(f"{where}: only a decisions section takes `act`")
    spec = cast("Mapping[str, object]", raw)
    plain: object = spec.get("plain", False)
    if not isinstance(plain, bool):
        raise PagesConfigError(f"{where}: `plain` is true or false")
    if plain and kind != "table":
        raise PagesConfigError(f"{where}: only a table can be `plain`")
    raw_fields: object = spec.get("fields")
    labels: tuple[tuple[str, str], ...] = ()
    signed: tuple[str, ...] = ()
    if plain:
        fields, labels, signed = _plain_fields(raw_fields, where)
    else:
        fields = _names(raw_fields, where, "fields")
    if kind == "chart" and not 1 <= len(fields) <= MAX_CHART_SERIES:
        raise PagesConfigError(
            f"{where}: `fields` must name exactly one timeseries field, or 2..{MAX_CHART_SERIES} "
            "of the same row to overlay as lines")
    if kind == "chart" and len(set(fields)) != len(fields):
        raise PagesConfigError(f"{where}: `fields` names a series twice")
    annotations = _annotations(raw.get("annotations"), where)
    if annotations and kind != "chart":
        raise PagesConfigError(f"{where}: only a chart takes `annotations`")
    x_raw: object = cast("Mapping[str, object]", raw).get("x")
    x = str(x_raw).strip() if x_raw else None
    if x is not None and kind not in ("chart", "grid"):
        raise PagesConfigError(f"{where}: only a chart or a grid takes `x`")
    y_raw: object = cast("Mapping[str, object]", raw).get("y")
    y = str(y_raw).strip() if y_raw else None
    columns: int = DEFAULT_GRID_COLUMNS
    if kind == "grid":
        if x is None or y is None:
            raise PagesConfigError(f"{where}: a grid takes `x` (its columns) and `y` (its rows)")
        if x == y:
            raise PagesConfigError(f"{where}: a grid's `x` and `y` must be different fields")
        if len(fields) != 1 or fields[0] in (x, y):
            raise PagesConfigError(f"{where}: a grid takes exactly one field for its cells, "
                                   "other than `x` and `y`")
        stray = [k for k in ("sort", "sort_order", "sortable", "annotations", "tick")
                 if k in raw]
        if stray:
            raise PagesConfigError(
                f"{where}: {', '.join(f'`{k}`' for k in stray)} does not apply to a grid "
                "(its rows sort by the newest column)")
        cols_raw: object = cast("Mapping[str, object]", raw).get("columns", DEFAULT_GRID_COLUMNS)
        if (isinstance(cols_raw, bool) or not isinstance(cols_raw, int)
                or not 1 <= cols_raw <= MAX_GRID_COLUMNS):
            raise PagesConfigError(f"{where}: `columns` must be an integer 1..{MAX_GRID_COLUMNS}")
        columns = cols_raw
    elif y is not None or "columns" in raw:
        raise PagesConfigError(f"{where}: only a grid takes `y` and `columns`")
    if x is not None and annotations:
        raise PagesConfigError(f"{where}: a chart across rows (`x`) takes no `annotations`")
    if x is not None and x in fields:
        raise PagesConfigError(f"{where}: `x` names a field that is also a series")
    limit = raw.get("limit", DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise PagesConfigError(f"{where}: `limit` must be an integer 1..{MAX_LIMIT}")
    sort = str(raw["sort"]).strip() if raw.get("sort") else None
    if sort is not None and kind != "table" and x is None:
        raise PagesConfigError(f"{where}: only a table, or a chart across rows (`x`), "
                               "takes `sort`")
    sort_order = _names(raw.get("sort_order"), where, "sort_order")
    if sort_order and (sort is None or sort.startswith("-")):
        raise PagesConfigError(
            f"{where}: `sort_order` lists the values of an ascending `sort` field; "
            "declare `sort: <field>` without `-`")
    if len(set(sort_order)) != len(sort_order):
        raise PagesConfigError(f"{where}: `sort_order` names a value twice")
    sortable = _names(raw.get("sortable"), where, "sortable")
    if sortable and kind != "table":
        raise PagesConfigError(f"{where}: only a table takes `sortable`")
    if any(n.startswith("-") for n in sortable):
        raise PagesConfigError(f"{where}: `sortable` names fields; the URL picks the direction")
    unshown = [n for n in sortable if fields and n not in fields]
    if unshown:
        raise PagesConfigError(
            f"{where}: `sortable` field(s) {', '.join(unshown)} are not among the table's `fields`")
    tick = _tick(raw.get("tick"), where, kind, ticks)
    return Section(type=kind, title=title, select=select, fields=fields,
                   sort=sort, limit=limit, sort_order=sort_order, empty=empty,
                   annotations=annotations, sortable=sortable, tick=tick, x=x,
                   y=y, columns=columns, plain=plain, labels=labels, signed=signed)


_WRITE_PATH = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9._~/-]{0,199}$")
_BODY_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def is_console_route(path: str) -> bool:
    """Whether ``path`` is one the console itself serves (a view or an asset).
    A declared write path must not be: the console never receives a write."""
    from ..server import router

    try:
        router.resolve(path)
    except router.UnknownRoute:
        return path.split("?", 1)[0] in _STATIC_PATHS
    return True


#: The console's own static asset paths (`server/app.py`), which `resolve` does not route.
_STATIC_PATHS = frozenset({"/styles.css", "/console.js", "/charts.js"})


def _write_path(raw: object, where: str) -> str:
    post = str(raw or "").strip()
    if not _WRITE_PATH.match(post) or "//" in post or "/../" in post or post.endswith("/.."):
        raise PagesConfigError(
            f"{where}: `act.post` must be a same-origin absolute path such as "
            f"/back-office/decide, not {post!r}")
    if post.startswith("/vendor/") or is_console_route(post):
        raise PagesConfigError(
            f"{where}: `act.post` {post!r} is a console route; a page posts to the writer that "
            "owns the record, never to the console")
    return post


def _body_field(raw: object, where: str, what: str) -> str:
    name = str(raw or "").strip()
    if not _BODY_FIELD.match(name):
        raise PagesConfigError(f"{where}: `{what}` must be a field name like `verdict`, "
                               f"not {name!r}")
    return name


def _mapping(raw: object, where: str, what: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise PagesConfigError(f"{where}: {what} must be a mapping")
    return cast("Mapping[str, object]", raw)


def _items(raw: object, where: str, what: str) -> list[object]:
    if not isinstance(raw, list) or not raw:
        raise PagesConfigError(f"{where}: {what}")
    return cast("list[object]", raw)


def _choice(raw: object, where: str) -> Choice:
    entry = _mapping(raw, where, "a button")
    return Choice(value=_text(entry, "value", where), label=_text(entry, "label", where),
                  primary=bool(entry.get("primary", False)))


def _change(raw: object, at: str) -> tuple[Choice, tuple[tuple[str, tuple[tuple[str, str], ...]], ...]]:
    """The "change" button and the selects it opens: body field -> (value, label) options."""
    change = _choice(raw, f"{at} change")
    selects = cast("Mapping[str, object]", raw).get("fields")
    if not isinstance(selects, Mapping) or not selects:
        raise PagesConfigError(f"{at} change: `fields` must map a body field to its options")
    out: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for name, opts in cast("Mapping[object, object]", selects).items():
        fname = _body_field(name, f"{at} change", "fields")
        where = f"{at} change field {fname}"
        options = [_choice(o, where) for o in _items(opts, where, "must list options")]
        out.append((fname, tuple((c.value, c.label) for c in options)))
    return change, tuple(out)


def _decisions(raw: Mapping[str, object], where: str, title: str, select: Selector,
               empty: str | None) -> Section:
    """A `decisions` section: rows as cards, each with the declared buttons."""
    stray = [k for k in ("sortable", "sort_order", "annotations", "x", "tick") if k in raw]
    if stray:
        raise PagesConfigError(
            f"{where}: {', '.join(f'`{k}`' for k in stray)} does not apply to a decisions section")
    if not isinstance(raw.get("act"), Mapping):
        raise PagesConfigError(f"{where}: `act` must declare where a decision is posted")
    at = f"{where} act"
    act_raw = _mapping(raw.get("act"), at, "`act`")
    known = {"post", "key", "send", "choice_field", "choices", "change", "note", "headline"}
    unknown = sorted(set(act_raw) - known)
    if unknown:
        raise PagesConfigError(f"{at}: unknown key(s) {', '.join(unknown)}")
    key = _body_field(act_raw.get("key"), at, "key")
    send_raw = act_raw.get("send")
    send = tuple(_body_field(n, at, "send")
                 for n in (cast("list[object]", send_raw) if isinstance(send_raw, list) else []))
    choice_field = _body_field(act_raw.get("choice_field") or "choice", at, "choice_field")
    choices = tuple(_choice(c, f"{at} choice #{n}") for n, c in enumerate(
        _items(act_raw.get("choices"), at, "`choices` must list at least one button"), start=1))
    change: Choice | None = None
    change_fields: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    if act_raw.get("change") is not None:
        change, change_fields = _change(act_raw["change"], at)
    note = bool(act_raw.get("note", True))
    posted = [key, *send, choice_field, *(n for n, _ in change_fields)]
    if note:
        posted.append("note")
    if len(set(posted)) != len(posted):
        raise PagesConfigError(f"{at}: a body field is posted twice: {posted}")
    values = [c.value for c in choices] + ([change.value] if change else [])
    if len(set(values)) != len(values):
        raise PagesConfigError(f"{at}: two buttons post the same value")
    headline = str(act_raw.get("headline") or "").strip() or None
    labels = _also(raw.get("fields"), where)
    limit = raw.get("limit", DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise PagesConfigError(f"{where}: `limit` must be an integer 1..{MAX_LIMIT}")
    sort = str(raw["sort"]).strip() if raw.get("sort") else None
    act = Act(post=_write_path(act_raw.get("post"), at), key=key, send=send,
              choice_field=choice_field, choices=choices, change=change,
              change_fields=change_fields, note=note, headline=headline)
    return Section(type="decisions", title=title, select=select,
                   fields=tuple(n for n, _ in labels), sort=sort, limit=limit, empty=empty,
                   act=act, labels=labels)


def parse(raw: Any, feeds: set[str] | None = None,
          ticks: set[str] | None = None) -> tuple[Page, ...]:
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
        if SORT_PARAM in variables:
            raise PagesConfigError(f"{where}: `{SORT_PARAM}` is a table's sort, not a variable")
        default_newest = _names(p["default_newest"] if "default_newest" in p else None,
                                where, "default_newest")
        stray = [v for v in default_newest if v not in variables]
        if stray:
            raise PagesConfigError(f"{where}: `default_newest` names {stray}, "
                                   "which are not in `variables`")
        section = str(p.get("section") or "other").strip().lower()
        if not _SECTION.match(section):
            raise PagesConfigError(f"{where}: `section` must be a short lowercase name "
                                   "(letters, digits, spaces, dashes)")
        product = str(p.get("product") or "").strip().lower() or None
        if product is not None and not _PAGE_ID.match(product):
            raise PagesConfigError(f"{where}: `product` must be a declared product id")
        pages.append(Page(
            id=pid, title=_text(p, "title", where), question=_text(p, "question", where),
            section=section, product=product,
            sections=tuple(_section(s, f"{where} section #{n}", feeds, ticks)
                           for n, s in enumerate(sections, start=1)),
            range=rng, variables=variables, links=_links(p.get("links"), where),
            default_newest=default_newest))
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
    if not all((k in facets) if v == ANY else facets.get(k) == v for k, v in sel.facets):
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


def _also(raw: Any, where: str) -> tuple[tuple[str, str], ...]:
    """A tile's `also:` list: each entry a field name, or `{field, label}`."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise PagesConfigError(f"{where}: `also` must be a list of fields")
    out = []
    for n, a in enumerate(raw, start=1):
        if isinstance(a, str) and a.strip():
            out.append((a.strip(), a.strip()))
        elif isinstance(a, Mapping):
            name = _text(a, "field", f"{where} also #{n}")
            out.append((name, str(a.get("label") or name).strip()))
        else:
            raise PagesConfigError(
                f"{where} also #{n}: must be a field name or a mapping with `field`")
    return tuple(out)


def _tile(index: Any, tile: Tile, variables: Mapping[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {"label": tile.label, "selector": tile.select.text,
                           "field": tile.field}
    matched = _select(index, tile.select, variables)
    if not matched:
        return {**out, "id": None, "absent": "no row matches the selector"}
    # The newest row that CARRIES the field: a selector may match several
    # sources (a summary row and a ladder row on one pane), and only one of
    # them declares the number this tile shows.
    rows = [e for e in matched if tile.field in _fields(e)] or matched
    ent = rows[0]
    out.update(id=ent.id, url=ent.route, state=ent.state_value,
               as_of=ent.provenance.as_of, evidence=ent.provenance.evidence)
    f = _fields(ent).get(tile.field)
    if f is None:
        return {**out, "absent": f"the newest row carries no field {tile.field!r}"}
    out.update(value=f.value, text=format_value(f), unit=f.unit,
               baseline={"declared": f.baseline_declared, "value": f.baseline},
               defect=f.defect)
    whole = _fields(ent).get(f.of or "")
    if whole is not None:
        out["of"] = {"name": whole.name, "value": whole.value, "text": format_value(whole)}
    if tile.also:
        # The same row's other fields. A field the row does not carry says so
        # rather than vanishing, like the tile's own value.
        by_name = _fields(ent)
        out["also"] = [
            {"field": name, "label": label, "value": by_name[name].value,
             "text": format_value(by_name[name]), "unit": by_name[name].unit,
             # Carried like the tile's own value's: a declaration defect
             # (e.g. a count with no unit) is shown, not dropped (codex c142d47c).
             "defect": by_name[name].defect}
            if name in by_name else
            {"field": name, "label": label, "value": None, "text": "not carried",
             "unit": None, "defect": None}
            for name, label in tile.also]
    if f.comparable and _number(f.value) and _number(f.baseline):
        out["versus_baseline"] = ("above" if f.value > f.baseline else
                                  "below" if f.value < f.baseline else "at")
    return out


def _number(v: Any) -> bool:
    """A real number — never a bool, never a string that looks like one. A
    declared baseline may be text ("budget", "1.0"); comparing it to a number
    raised TypeError and took the whole home page down (2026-10-28)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _safe_tile(index: Any, tile: Tile, variables: Mapping[str, str]) -> dict[str, Any]:
    """One tile, isolated: a defect in one emitter's row shows on that tile and
    never removes the page the other tiles are on."""
    try:
        return _tile(index, tile, variables)
    except Exception as exc:  # noqa: BLE001 — rendered, not swallowed
        return {"label": tile.label, "selector": tile.select.text, "field": tile.field,
                "id": None, "absent": f"tile failed: {type(exc).__name__}: {exc}"}


def _sort_key(sort: str, order: tuple[str, ...] = ()) -> Callable[[dict[str, object]], tuple[object, ...]]:
    desc = sort.startswith("-")
    name = sort.lstrip("-")
    if order:
        rank = {v: i for i, v in enumerate(order)}

        def ranked(row: dict) -> tuple:
            v = row["fields"].get(name)
            # Listed values in declared order, then any unlisted value by its
            # text, then a row without the value.
            return ((0, rank[str(v)], "") if v is not None and str(v) in rank
                    else (1, 0, str(v)) if v is not None else (2, 0, ""))

        return ranked

    def key(row: dict) -> tuple:
        v = row["fields"].get(name)
        number = isinstance(v, (int, float)) and not isinstance(v, bool)
        # A row without the value sorts last in either direction.
        present = v is not None
        value = (float(v) if number else 0.0, "" if number else str(v))
        # `-` reverses the whole value, text included: negating only the number
        # left `-trading_day` listing ISO dates oldest first (aec#13053).
        return (not present, _Descending(value) if desc else value)

    return key


class _Descending:
    """A sort-key part that orders in reverse, for values (text) that cannot be negated."""

    __slots__ = ("value",)

    def __init__(self, value: tuple) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Descending) and self.value == other.value

    def __lt__(self, other: "_Descending") -> bool:
        return other.value < self.value


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


def _before_range(label: Any, since: datetime | None) -> bool:
    """Whether a point's label is a date or instant wholly before the range.

    A label that is not a date is never before anything: it stays."""
    if since is None:
        return False
    when = _instant(label)
    if when is not None and len(str(label).strip()) == 10:
        # A bare date is a whole day: it is inside the range when any part of
        # it is.
        when += timedelta(days=1)
    return when is not None and when <= since


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
            if _before_range(label, since):
                dropped += 1
                continue
            kept.append((label, y))
        points = kept
    whole = _fields(ent).get(f.of or "")
    return {"points": [[label, y] for label, y in points], "unit": f.unit,
            "outside_range": dropped,
            "reference": ({"name": whole.name, "value": whole.value}
                          if whole is not None else None)}


def _overlay(ent: Any, fields: tuple[str, ...], since: datetime | None) -> dict[str, Any]:
    """Several timeseries of ONE row on one axis (alpha-engine-config-I12798):
    a return beside the benchmarks it is measured against, each a line.

    The x axis is the union of every series' labels — chronological when every
    label is a date or instant, first-seen order otherwise — and each series
    carries one value per label, ``None`` where it has no point (a gap, never
    zero, and never interpolated). A field the row does not carry, or carries
    in a shape this console cannot read, is listed under ``missing`` with why
    and drawn as nothing; the other series still draw. Series in different
    units share no axis, so a mix renders as absent rather than as one
    misleading picture. Nothing is computed: each value is the row's own."""
    by_name = _fields(ent)
    read: list[tuple[str, Any, list]] = []
    missing: list[dict[str, str]] = []
    for name in fields:
        f = by_name.get(name)
        if f is None:
            missing.append({"field": name, "why": "the newest row carries no such field"})
            continue
        points = series_points(f.value) if f.render is Render.TIMESERIES else None
        if points is None:
            missing.append({"field": name,
                            "why": "not a timeseries this console can read"})
            continue
        read.append((name, f, points))
    if not read:
        return {"absent": "the newest row carries none of the fields as a timeseries",
                "missing": missing}
    units = sorted({f.unit or "" for _, f, _ in read})
    if len(units) > 1:
        return {"absent": "the fields carry different units ("
                + ", ".join(repr(u) for u in units) + "); an overlay needs one axis",
                "missing": missing}
    labels: dict[str, None] = {}
    for _, _, points in read:
        for label, _ in points:
            labels.setdefault(label, None)
    order = list(labels)
    if order and all(_instant(label) is not None for label in order):
        order.sort(key=lambda label: (_instant(label), label))
    shown = [label for label in order if not _before_range(label, since)]
    dropped = len(order) - len(shown)
    series = []
    for name, _, points in read:
        values = dict(points)
        series.append({"field": name, "values": [values.get(label) for label in shown]})
    return {"labels": shown, "series": series, "missing": missing,
            "unit": units[0] or None, "outside_range": dropped}


def _as_number(v: object) -> float | None:
    """A numeric value as a float; anything else (text, a bool, None) is None."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _across(rows: Sequence[Entity], s: Section) -> dict[str, object]:
    """A chart ACROSS rows (`x`): one point per matched row, at the row's own
    `x` value, for each declared numeric field.

    When every `x` value is a date or instant the points are a time axis: in
    date order, drawn as lines, and two rows on one date keep the newest (the
    rows arrive newest first). Otherwise each row is a bar group, in `sort`
    order (the declared order of the rows otherwise), at most `limit`. A row
    without an `x` value is counted, never placed. A value that is not a
    number is a gap, never zero. Series in different units share no axis."""
    x = s.x or ""
    units: set[str] = set()
    sort_name = s.sort.lstrip("-") if s.sort else None
    points: list[tuple[str, dict[str, object], Entity]] = []
    unplaced = 0
    for e in rows:
        raw_fields = cast("Mapping[str, object] | None", e.detail.get("fields"))
        by_name: dict[str, Field] = {f.name: f for f in parse_fields(raw_fields)}
        xf = by_name.get(x)
        xv: object = xf.value if xf is not None else None
        if xv is None or xv == "":
            unplaced += 1
            continue
        values: dict[str, object] = {}
        for name in (*s.fields, *((sort_name,) if sort_name else ())):
            f = by_name.get(name)
            v: object = f.value if f is not None else None
            values[name] = v
            if f is not None and name in s.fields and _as_number(v) is not None:
                units.add(f.unit or "")
        points.append((str(xv), {"fields": values}, e))
    if not points:
        return {"absent": f"no matched row carries {x!r}", "unplaced": unplaced}
    if len(units) > 1:
        return {"absent": "the fields carry different units ("
                + ", ".join(repr(u) for u in sorted(units)) + "); one chart needs one axis"}
    timed = all(_instant(label) is not None for label, _, _ in points)
    chosen: list[tuple[str, dict[str, object], Entity]]
    if timed:
        newest: dict[str, tuple[str, dict[str, object], Entity]] = {}
        for p in points:
            newest.setdefault(p[0], p)
        chosen = sorted(newest.values(), key=lambda p: (_instant(p[0]), p[0]))
    else:
        ordered = list(points)
        if s.sort:
            key = _sort_key(s.sort)
            ordered.sort(key=lambda p: key(p[1]))
        chosen = ordered[:s.limit]

    def value(row: dict[str, object], name: str) -> float | None:
        fields = cast("dict[str, object]", row["fields"])
        return _as_number(fields.get(name))

    series = [{"field": name, "values": [value(r, name) for _, r, _ in chosen]}
              for name in s.fields]
    floor = datetime.min.replace(tzinfo=timezone.utc)
    newest_row = max((e for _, _, e in chosen),
                     key=lambda e: _instant(e.provenance.as_of) or floor)
    return {"layout": "lines" if timed else "bars",
            "labels": [label for label, _, _ in chosen],
            "urls": [e.route for _, _, e in chosen],
            "series": series, "unit": (next(iter(units)) if units else "") or None,
            "matched": len(rows), "shown": len(chosen), "unplaced": unplaced,
            "as_of": newest_row.provenance.as_of}


def _grid(rows: Sequence[Entity], s: Section, marked: str | None) -> dict[str, object]:
    """A grid: one row per `y` value, one column per `x` value (the newest
    `columns` of them, oldest first), each cell the declared field of the row
    carrying that pair. Two rows on one cell keep the first (rows arrive
    newest first). A cell no row fills is absent, which is not zero: on an
    order book, a name not in the book that day. Rows sort by the newest
    column's value, largest first, then by their largest value anywhere, so
    a name that just left the book stays beside the ones it left; `limit`
    of them are shown. `marked` is the column a page variable picked."""
    x, y, name = s.x or "", s.y or "", s.fields[0]
    cells: dict[tuple[str, str], object] = {}
    urls: dict[tuple[str, str], str] = {}
    unit: str | None = None
    unplaced = 0
    for e in rows:
        raw_fields = cast("Mapping[str, object] | None", e.detail.get("fields"))
        by_name: dict[str, Field] = {f.name: f for f in parse_fields(raw_fields)}
        xf, yf, vf = by_name.get(x), by_name.get(y), by_name.get(name)
        xv = xf.value if xf is not None else None
        yv = yf.value if yf is not None else None
        if xv in (None, "") or yv in (None, ""):
            unplaced += 1
            continue
        cell = (str(yv), str(xv))
        if cell in cells:
            continue
        cells[cell] = vf.value if vf is not None else None
        urls[cell] = e.route
        if vf is not None and unit is None and vf.unit:
            unit = vf.unit
    if not cells:
        return {"absent": f"no matched row carries both {x!r} and {y!r}", "unplaced": unplaced}
    # ISO dates (the common `x`) sort as text in date order.
    all_columns = sorted({c for _, c in cells})
    shown_columns = all_columns[-s.columns:]
    newest = shown_columns[-1]

    def number(r: str, c: str) -> float | None:
        return _as_number(cells.get((r, c)))

    names = sorted({r for r, c in cells if c in shown_columns})
    names.sort(key=lambda r: (-(number(r, newest) or 0.0),
                              -max((number(r, c) or 0.0) for c in shown_columns), r))
    values = [n for r in names for c in shown_columns if (n := number(r, c)) is not None]
    return {"columns": shown_columns, "marked": marked if marked in shown_columns else None,
            "rows": [{"label": r,
                      "cells": [{"value": number(r, c),
                                 "url": urls.get((r, c))} for c in shown_columns]}
                     for r in names[:s.limit]],
            "unit": unit, "max": max(values, default=0.0), "min": min(values, default=0.0),
            "matched": len(names), "shown": min(len(names), s.limit),
            "columns_matched": len(all_columns), "unplaced": unplaced}


def _day(value: Any) -> str | None:
    """The UTC calendar day of a date or instant, or None when it is neither."""
    when = _instant(value)
    return when.date().isoformat() if when is not None else None


def _marks(index: Any, specs: tuple[Annotation, ...], variables: Mapping[str, str],
           points: list) -> tuple[list[dict[str, Any]], int, int]:
    """Place each declared event on the chart's dates.

    A mark sits on the last point whose day is on or before the event's day,
    so an event on a non-trading day lands on the bar before it. An event
    before the first point or after the last is OUTSIDE the chart and counted,
    never pinned to an edge; an event whose `at` is not a date is UNDATED and
    counted too, never dropped silently. Returns (marks, outside, undated)."""
    days = [_day(label) for label, _ in points]
    known = [d for d in days if d is not None]
    marks: list[dict[str, Any]] = []
    outside = undated = 0
    for spec in specs:
        for ent in _select(index, spec.select, variables):
            by_name = _fields(ent)
            raw = by_name[spec.at].value if spec.at in by_name else None
            day = _day(raw)
            if day is None:
                undated += 1
                continue
            # By day, never by position: a series is not assumed to arrive in
            # chronological order (codex 25e8f237f4fba4b3).
            if not known or day < min(known) or day > max(known):
                outside += 1
                continue
            slot = max((d, i) for i, d in enumerate(days) if d is not None and d <= day)[1]
            named = by_name.get(spec.text) if spec.text else None
            text = (str(named.value) if named is not None and named.value is not None
                    else str(ent.detail.get(LABEL_DETAIL) or ent.id))
            marks.append({"label": spec.label, "at": str(raw), "day": day, "slot": slot,
                          "point": points[slot][0], "text": text,
                          "id": ent.id, "url": ent.route})
    marks.sort(key=lambda m: (m["day"], m["label"], m["id"]))
    return marks, outside, undated


def _requested_sort(s: Section, requested: str | None) -> str | None:
    """The URL's `sort` when this table lists its field as sortable, else None."""
    if not requested or not s.sortable:
        return None
    return requested if requested.lstrip("-") in s.sortable else None


def _section_doc(index: Any, s: Section, variables: Mapping[str, str],
                 since: datetime | None, requested_sort: str | None = None) -> dict[str, Any]:
    from ..render.html import is_exception, state_rank

    doc: dict[str, Any] = {"type": s.type, "title": s.title}
    if s.empty is not None:
        doc["empty"] = s.empty
    if s.type == "text":
        doc["body"] = s.body
        return doc
    if s.type == "tiles":
        doc["tiles"] = [_safe_tile(index, t, variables) for t in s.tiles]
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
    if s.type == "grid":
        # A grid spans its `x` values, so a page variable on that same facet
        # (the order book's day picker) marks a column rather than filtering
        # the grid down to one.
        spanned = {k: v for k, v in variables.items() if k != s.x}
        matched_rows = cast("list[Entity]", _select(index, s.select, spanned))
        doc.update(x=s.x, y=s.y, field=s.fields[0],
                   **_grid([e for e in matched_rows if _in_range(e, since)], s,
                           variables.get(s.x or "")))
        return doc
    rows = _select(index, s.select, variables)
    if s.type == "chart" and s.x is not None:
        matched = cast("list[Entity]", rows)
        across = [e for e in matched if _in_range(e, since)]
        doc.update(x=s.x, fields=list(s.fields), **_across(across, s))
        return doc
    if s.type == "chart":
        overlay = len(s.fields) > 1
        if overlay:
            doc["fields"] = list(s.fields)
        else:
            doc["field"] = s.fields[0]
        if not rows:
            doc["absent"] = "no row matches the selector"
            return doc
        ent = rows[0]
        doc.update(id=ent.id, url=ent.route, as_of=ent.provenance.as_of,
                   **(_overlay(ent, s.fields, since) if overlay
                      else _chart(ent, s.fields[0], since)))
        points = ([[label, None] for label in doc["labels"]] if "labels" in doc
                  else doc.get("points"))
        if s.annotations and points is not None:
            marks, outside, undated = _marks(index, s.annotations, variables, points)
            doc.update(annotations=marks, annotations_outside=outside,
                       annotations_undated=undated)
        return doc
    in_range = [e for e in rows if _in_range(e, since)]
    if s.type == "decisions":
        return _decisions_doc(doc, s, len(cast("list[Entity]", rows)),
                              cast("list[Entity]", in_range))
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
    chosen = _requested_sort(s, requested_sort)
    # The sort field is read even when it is not shown: an order book sorts
    # by its own `book_position` without printing it, and a key read only
    # from the shown columns left every such table in index order.
    keys = [n.lstrip("-") for n in (chosen, s.sort) if n]
    shown_and_keys = [*columns, *(k for k in keys if k not in columns)]
    table = [_row(e, shown_and_keys) for e in cast("list[Entity]", in_range)]
    if s.sortable:
        doc["sort"] = {"value": chosen or s.sort, "default": s.sort,
                       "sortable": list(s.sortable)}
    if chosen:
        # A declared value order applies to its own ascending field only.
        table.sort(key=_sort_key(chosen, s.sort_order if chosen == s.sort else ()))
    elif s.sort:
        table.sort(key=_sort_key(s.sort, s.sort_order))
    else:
        table.sort(key=lambda r: state_rank(r["state"]))  # stable: newest within a state
    if s.plain:
        doc.update(plain=True, labels=dict(s.labels), signed=list(s.signed))
    doc.update(columns=columns, matched=len(rows), total=len(table),
               rows=table[:s.limit], outside_range=len(rows) - len(in_range))
    if s.tick is not None:
        from . import ticks

        name, key = s.tick
        found = ticks.store(index, name)
        doc["tick"] = {"list": name, "key": key,
                       "label": found.declaration.label if found else "done",
                       "writable": found is not None and found.error is None,
                       "error": found.error if found else "not read"}
        by_id = {e.id: e for e in in_range}
        for r in doc["rows"]:
            value = _fields(by_id[r["id"]]).get(key) if r["id"] in by_id else None
            k = str(value.value) if value is not None and value.value not in (None, "") else None
            r["tick"] = {"key": k, "done": bool(found and k and found.done(k))}
    return doc


def _decisions_doc(doc: dict[str, object], s: Section, matched: int,
                   in_range: list[Entity]) -> dict[str, object]:
    """A decisions section's cards: each row with its headline, its labelled
    fields and the values its card posts, plus the declared write."""
    act = s.act
    assert act is not None
    wanted = [*(n for n, _ in s.labels), act.key, *act.send]
    if act.headline:
        wanted.append(act.headline)
    if s.sort:
        wanted.append(s.sort.lstrip("-"))
    columns = list(dict.fromkeys(wanted))
    cards: list[dict[str, object]] = [_row(e, columns) for e in in_range]
    if s.sort:
        cards.sort(key=_sort_key(s.sort, ()))
    change: dict[str, object] | None = None
    if act.change is not None:
        change = {"value": act.change.value, "label": act.change.label,
                  "fields": [{"name": n, "options": [list(o) for o in opts]}
                             for n, opts in act.change_fields]}
    doc.update(matched=matched, total=len(cards), rows=cards[:s.limit],
               outside_range=matched - len(in_range),
               labels=[list(pair) for pair in s.labels],
               act={"post": act.post, "key": act.key, "send": list(act.send),
                    "choice_field": act.choice_field, "headline": act.headline,
                    "note": act.note, "change": change,
                    "choices": [{"value": c.value, "label": c.label, "primary": c.primary}
                                for c in act.choices]})
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
    requested_sort = str(params.get(SORT_PARAM) or "").strip() or None
    options = _variable_options(index, p)
    for v in p.default_newest:
        if v not in variables and options[v]:
            variables[v] = options[v][-1]
    # The page's own state minus the sort, for a header's link to carry over.
    query = {**({RANGE_PARAM: rng} if rng != p.range else {}), **variables}
    return {
        "id": p.id, "title": p.title, "url": p.url, "question": p.question,
        "links": [{"title": link.title, "url": link.url} for link in p.links],
        "range": {"value": rng, "default": p.range, "options": list(RANGES),
                  "since": since.strftime("%Y-%m-%dT%H:%M:%SZ") if since else None},
        "variables": [{"name": v, "value": variables.get(v), "options": options[v],
                       "all": v not in p.default_newest}
                      for v in p.variables],
        "query": query,
        SORT_PARAM: requested_sort,
        "sections": [_section_doc(index, s, variables, since, requested_sort) for s in p.sections],
    }


def tick_keys(index: Any, name: str) -> set[str]:
    """Every key a table declaring tick list ``name`` can show: the bound on
    what a POST may tick (`index/ticks.py`)."""
    out: set[str] = set()
    for p in declared(index):
        for s in p.sections:
            if s.tick is None or s.tick[0] != name or s.select is None:
                continue
            for ent in _select(index, s.select, {}):
                f = _fields(ent).get(s.tick[1])
                if f is not None and f.value not in (None, ""):
                    out.add(str(f.value))
    return out
