"""The home page's declared summary — system health by area, then headline
tiles (`products/console.md` CN-4.3, amended 2026-10-28).

Brian, 2026-10-28 18:40Z: "We should be able to tell the health of the system
at a glance before a summary of the exceptions ... the home page should be
summary info only with links to more detailed pages." So home opens with ONE
TILE PER AREA (trading, models, pipelines, ...): the area's worst state as a
word, how many of its rows are not healthy out of how many, and its stalest
row's as-of — each linked to the area's own page.

**Areas are declared, not hard-coded.** A `home:` config block names each
area and the selectors (kind and facets, any-of) whose rows it owns. A row
belongs to the FIRST area that selects it, so the tiles partition the fleet. A row
that no area claims is counted in an **unassigned** tile — never dropped, so a
new source shows up on home the day it is onboarded, before anyone files it.

**No aggregate green.** Each tile carries its own area's state; nothing rolls
several areas into one verdict. An all-clear is every tile reading 0.

Pure over the built index and its landing model, per request, nothing stored
(§5.6).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from . import pages
from .pages import PagesConfigError, Selector, Tile

_ATTR = "_console_home"
UNASSIGNED = "unassigned"


class HomeConfigError(PagesConfigError):
    """A `home:` declaration this build cannot evaluate; fails the build by name."""


@dataclass(frozen=True)
class Area:
    id: str
    title: str
    link: str
    selects: tuple[Selector, ...]


@dataclass(frozen=True)
class TileGroup:
    """A titled row of headline tiles that answers one question, e.g. "trading
    today" (Brian 2026-11-04: home must answer what he asks, not list counts)."""
    title: str
    tiles: tuple[Tile, ...]
    link: str | None = None


@dataclass(frozen=True)
class Notes:
    """A short list of rows rendered near the top of home, each with labelled
    lines (Brian 2026-11-04: a daily "things to know" from the console
    reviewer, each saying the fix, what he must do, and whether the evening
    sweep has it). Rows are the newest batch: those sharing the newest as-of."""
    title: str
    select: Selector
    fields: tuple[tuple[str, str], ...] = ()
    severity_field: str | None = None
    link_field: str | None = None
    sort: str | None = None
    limit: int = 8
    stale_hours: float = 30.0
    empty: str = "nothing published yet"


@dataclass(frozen=True)
class Home:
    areas: tuple[Area, ...] = ()
    tiles: tuple[Tile, ...] = ()
    groups: tuple[TileGroup, ...] = ()
    notes: Notes | None = None


EMPTY = Home()


def parse(raw: Any) -> Home:
    if raw is None:
        return EMPTY
    if not isinstance(raw, Mapping):
        raise HomeConfigError("`home:` must be a mapping with `areas:` and `tiles:`")
    areas, ids = [], set()
    for n, a in enumerate(raw.get("areas") or [], start=1):
        where = f"home area #{n}"
        if not isinstance(a, Mapping):
            raise HomeConfigError(f"{where}: must be a mapping")
        aid = pages._text(a, "id", where)
        where = f"home area {aid!r}"
        if aid in ids or aid == UNASSIGNED:
            raise HomeConfigError(f"{where}: declared twice, or uses the reserved id")
        ids.add(aid)
        selects = a.get("select")
        if isinstance(selects, Mapping):
            selects = [selects]
        if not isinstance(selects, list) or not selects:
            raise HomeConfigError(f"{where}: `select` must be one selector or a list")
        link = pages._text(a, "link", where)
        if not link.startswith("/"):
            raise HomeConfigError(f"{where}: `link` must be a console path")
        areas.append(Area(id=aid, title=pages._text(a, "title", where), link=link,
                          selects=tuple(pages._selector(s, f"{where} select #{i}", kind_only=True)
                                        for i, s in enumerate(selects, start=1))))
    section = pages._section({"type": "tiles", "tiles": raw["tiles"]}, "home tiles", None) \
        if raw.get("tiles") else None
    groups = []
    for n, g in enumerate(raw.get("groups") or [], start=1):
        where = f"home group #{n}"
        if not isinstance(g, Mapping):
            raise HomeConfigError(f"{where}: must be a mapping")
        title = pages._text(g, "title", where)
        link = g.get("link")
        if link is not None and not (isinstance(link, str) and link.startswith("/")):
            raise HomeConfigError(f"home group {title!r}: `link` must be a console path")
        if not g.get("tiles"):
            raise HomeConfigError(f"home group {title!r}: `tiles` must list at least one tile")
        sec = pages._section({"type": "tiles", "tiles": g["tiles"]},
                             f"home group {title!r}", None)
        groups.append(TileGroup(title=title, tiles=sec.tiles, link=link))
    return Home(areas=tuple(areas), tiles=section.tiles if section else (),
                groups=tuple(groups), notes=_notes(raw.get("notes")))


def _notes(raw: Any) -> Notes | None:
    if raw is None:
        return None
    where = "home notes"
    if not isinstance(raw, Mapping):
        raise HomeConfigError(f"{where}: must be a mapping")
    fields = []
    for n, f in enumerate(raw.get("fields") or [], start=1):
        if isinstance(f, str) and f.strip():
            fields.append((f.strip(), f.strip()))
        elif isinstance(f, Mapping):
            name = pages._text(f, "field", f"{where} field #{n}")
            fields.append((name, str(f.get("label") or name).strip()))
        else:
            raise HomeConfigError(f"{where} field #{n}: a field name or {{field, label}}")
    try:
        limit = int(raw.get("limit", 8))
        stale = float(raw.get("stale_hours", 30))
    except (TypeError, ValueError) as exc:
        raise HomeConfigError(f"{where}: `limit` and `stale_hours` must be numbers") from exc
    if limit < 1 or stale <= 0:
        raise HomeConfigError(f"{where}: `limit` and `stale_hours` must be positive")
    return Notes(
        title=pages._text(raw, "title", where),
        select=pages._selector(raw.get("select"), f"{where} select"),
        fields=tuple(fields),
        severity_field=(str(raw["severity_field"]) if raw.get("severity_field") else None),
        link_field=(str(raw["link_field"]) if raw.get("link_field") else None),
        sort=(str(raw["sort"]) if raw.get("sort") else None),
        limit=limit, stale_hours=stale,
        empty=str(raw.get("empty") or "nothing published yet"))


def attach(index: Any, home: Home) -> None:
    setattr(index, _ATTR, home)


def declared(index: Any) -> Home:
    return getattr(index, _ATTR, EMPTY)


def _instant(value: Any) -> datetime | None:
    return pages._instant(value)


def _health(aid: str, title: str, link: str, rows: list, bad: list) -> dict[str, Any]:
    from ..model.kinds import Kind
    from ..render.html import state_rank

    # The worst state over the rows the exception list counts — never over
    # every row. A superseded FAILED run that a newer run already replaced is
    # not an exception, and a tile reading "FAILED · 0 not healthy" (live
    # 2026-10-29) contradicts itself. No exception and some rows is HEALTHY;
    # no rows at all stays None ("no rows"), never green.
    worst = (min((e.state_value for e in bad), key=state_rank) if bad
             else ("HEALTHY" if rows else None))
    # Stalest over rows that state a CURRENT fact. A run is one dated event in
    # a history, so its age says nothing about whether the area is current —
    # only when an area has nothing but runs does its newest run stand in.
    current = [e for e in rows if e.kind is not Kind.RUN]
    stamped = [(w, e) for e in (current or rows)
               if (w := _instant(e.provenance.as_of)) is not None]
    if current:
        stalest = min(stamped, key=lambda p: p[0]) if stamped else None
    else:
        stalest = max(stamped, key=lambda p: p[0]) if stamped else None
    return {
        "id": aid, "title": title, "url": link, "rows": len(rows),
        "not_healthy": len(bad), "worst": worst,
        "stalest": ({"id": stalest[1].id, "as_of": stalest[1].provenance.as_of,
                     "basis": "row" if current else "latest run"}
                    if stalest else None),
        "unstamped": sum(1 for e in (current or rows) if _instant(e.provenance.as_of) is None),
    }


def health(index: Any, exceptions: list) -> list[dict[str, Any]]:
    """One entry per declared area, then the unassigned area. `exceptions`
    is the landing model's list, so home's counts are the same counts the
    exception summary and `/exceptions` show."""
    h = declared(index)
    owned: set[str] = set()
    bad_ids = {e.id for e in exceptions}
    out = []
    every = list(index.all())
    for a in h.areas:
        # FIRST declared area wins a row: tiles partition the fleet, so their
        # not-healthy counts sum to the exception total and no row is red on
        # two tiles at once. Order areas specific-first, catch-alls last.
        rows = [e for e in every if e.id not in owned
                and any(pages._matches(e, s, {}) for s in a.selects)]
        owned.update(e.id for e in rows)
        out.append(_health(a.id, a.title, a.link, rows,
                           [e for e in rows if e.id in bad_ids]))
    rest = [e for e in every if e.id not in owned]
    out.append(_health(UNASSIGNED, "Unassigned", "/exceptions" if h.areas else "/dashboards",
                       rest, [e for e in rest if e.id in bad_ids]))
    return out


def headline(index: Any) -> list[dict[str, Any]]:
    return [pages._safe_tile(index, t, {}) for t in declared(index).tiles]


def headline_groups(index: Any) -> list[dict[str, Any]]:
    return [{"title": g.title, "link": g.link,
             "tiles": [pages._safe_tile(index, t, {}) for t in g.tiles]}
            for g in declared(index).groups]




def notes(index: Any, now: datetime | None = None) -> dict[str, Any] | None:
    """The declared notes list: the newest batch of matching rows, sorted and
    capped, with its as-of and whether it is older than `stale_hours`."""
    from datetime import timezone

    n = declared(index).notes
    if n is None:
        return None
    rows = pages._select(index, n.select, {})
    newest = rows[0].provenance.as_of if rows else None
    batch = [e for e in rows if e.provenance.as_of == newest]
    names = [f for f, _ in n.fields]
    extra = [x for x in (n.severity_field, n.link_field, n.sort) if x and x not in names]
    out = [pages._row(e, names + extra) for e in batch]
    if n.sort:
        out.sort(key=lambda r: (r["fields"].get(n.sort) is None, r["fields"].get(n.sort)))
    when = _instant(newest)
    now = now or datetime.now(timezone.utc)
    stale = when is not None and (now - when).total_seconds() > n.stale_hours * 3600
    return {"title": n.title, "as_of": newest, "stale": stale,
            "stale_hours": n.stale_hours, "empty": n.empty,
            "selector": n.select.text, "total": len(out),
            "labels": dict(n.fields), "severity_field": n.severity_field,
            "link_field": n.link_field, "rows": out[:n.limit]}
