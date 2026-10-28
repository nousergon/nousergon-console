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
class Home:
    areas: tuple[Area, ...] = ()
    tiles: tuple[Tile, ...] = ()


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
                          selects=tuple(pages._selector(s, f"{where} select #{i}")
                                        for i, s in enumerate(selects, start=1))))
    section = pages._section({"type": "tiles", "tiles": raw["tiles"]}, "home tiles", None) \
        if raw.get("tiles") else None
    return Home(areas=tuple(areas), tiles=section.tiles if section else ())


def attach(index: Any, home: Home) -> None:
    setattr(index, _ATTR, home)


def declared(index: Any) -> Home:
    return getattr(index, _ATTR, EMPTY)


def _instant(value: Any) -> datetime | None:
    return pages._instant(value)


def _health(aid: str, title: str, link: str, rows: list, bad: list) -> dict[str, Any]:
    from ..model.kinds import Kind
    from ..render.html import state_rank

    states = [e.state_value for e in (bad or rows)]
    worst = min(states, key=state_rank) if states else None
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
        rows = [e for e in every if any(pages._matches(e, s, {}) for s in a.selects)]
        owned.update(e.id for e in rows)
        out.append(_health(a.id, a.title, a.link, rows,
                           [e for e in rows if e.id in bad_ids]))
    rest = [e for e in every if e.id not in owned]
    out.append(_health(UNASSIGNED, "Unassigned", "/exceptions" if h.areas else "/dashboards",
                       rest, [e for e in rest if e.id in bad_ids]))
    return out


def headline(index: Any) -> list[dict[str, Any]]:
    return [pages._tile(index, t, {}) for t in declared(index).tiles]


