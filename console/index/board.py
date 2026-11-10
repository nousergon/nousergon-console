"""The planning board: Brian's ranked list of projects, at the top of /planner.

Brian, 2026-11-09: "a planning page in the operator console that shows the
list of all of the projects that we are working on at a time, and the
underlying issues for those projects ... I should be able to assign a row
ranking to each of them ... a date of the next deliverable ... and dates for
when the final phase is complete ... an archive at the bottom that shows all
of the projects that have been completed ... a checkbox so that we can check
that project to validate that it's complete."

**Two kinds of fact, two homes.**

- WHICH projects exist, and which epics and issues are theirs, is declared in
  the `planner:` config block (`index/planner.py`), the same rows the delivery
  calendar draws. Adding a project is a config change, reviewed like any other.
- Brian's INPUTS — rank, next-milestone date, final-phase date and the
  complete checkbox — are his to change from the page, so they live in one
  JSON object in S3 (`planner.board.store`), re-read on every build and before
  every write, exactly like a tick list (`index/ticks.py`). The console keeps
  nothing on the box. These dates are planning and budgeting dates, not the
  calendar's computed milestones; the calendar's dates are shown beside them
  as "plan" when the board has none, never copied into the store.

**Issues are read live.** A project's active work is every OPEN sub-issue of
its declared epics (one REST call per epic per cache window, on the box's App
token, never a person's API limit), plus its named `issues:` and its
milestones' required items while open. An epic or named issue that cannot be
read is named as unread, never shown as "no issues".

**The evening sweep reads this board** (Brian 2026-11-09: "projects and
priorities should come from the planner, priority should be assigned both by
project importance as well as how behind it is"). Each active row carries a
``status`` (off track / threatened / on track / no dates) and a
``sweep_position``: the row's rank, moved up `OFF_TRACK_LIFT` places when it is
off track or past a board date and `THREATENED_LIFT` when threatened. The rule
is deliberately small so a reader can redo it from the two numbers shown. In
return the board shows what the sweep did: each row's issues closed in the
last `RECENT_HOURS` hours.

**Ranks are a total order.** Saving a row with rank N moves it to position N
and renumbers every active project 1..n, so two projects never share a rank
and a rank never has a gap. A completed project leaves the order and is
listed in the archive with when and by whom; unticking it returns it to the
bottom of the order (or to the rank given with the untick).
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Mapping, TypedDict, cast

from ..adapters.git_host import SubIssue
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass

SOURCE_NAME = "planner-board"
SCHEMA_VERSION = 1
_ATTR = "_console_planner_board"
_EPICS_ATTR = "_console_planner_epics"
_STORE = re.compile(
    r"^s3://(?P<bucket>[a-z0-9][a-z0-9.-]{1,62})/(?P<key>[A-Za-z0-9/._-]{1,512}\.json)$")
PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
#: The label that marks an issue as being worked right now; listed first.
IN_PROGRESS_LABEL = "in-progress"
#: Open issues a board row lists before folding the rest.
SHOWN_ISSUES = 4
#: Places an off-track (or past-date) project moves up in the sweep order.
OFF_TRACK_LIFT = 3
#: Places a threatened project moves up in the sweep order.
THREATENED_LIFT = 1
#: The "what the sweep did" window: issues closed this recently.
RECENT_HOURS = 24
OFF_TRACK_WORD = "off track"
THREATENED_WORD = "threatened"
ON_TRACK_WORD = "on track"
NO_DATES_WORD = "no dates"

#: One saved row: rank, dates, complete and who/when, as raw JSON.
Entry = dict[str, object]
Getter = Callable[[str, str], "bytes | None"]
Putter = Callable[[str, str, bytes], None]


class BoardConfigError(ValueError):
    """A `planner.board` block this build cannot honour."""


class SaveRefused(Exception):
    """A board edit the handler will not save, with the reason and status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status: int = status


@dataclass(frozen=True)
class Declaration:
    bucket: str
    key: str
    #: Where each build publishes the whole /planner.json document, so readers
    #: that cannot reach the console (the cloud plan and nightly sessions)
    #: read the same board, ranks, statuses and sweep order (Director
    #: 2026-11-09). (bucket, key), or None when not published.
    publish: tuple[str, str] | None = None

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


@dataclass
class Store:
    declaration: Declaration
    entries: dict[str, Entry] = field(default_factory=lambda: dict[str, Entry]())
    read_at: str | None = None
    error: str | None = None
    #: The last publish of /planner.json: when, or why it failed.
    published_at: str | None = None
    publish_error: str | None = None


@dataclass(frozen=True)
class Epics:
    """What the last build read of every declared epic's sub-issues."""

    rows: Mapping[str, list[SubIssue]] = field(
        default_factory=lambda: dict[str, list[SubIssue]]())
    unread: tuple[str, ...] = ()


class BoardIssue(TypedDict):
    ref: str
    title: str | None
    url: str | None
    in_progress: bool
    blocker: str | None


class BoardRow(TypedDict):
    id: str
    title: str
    url: str
    tracker: str | None
    owner: str | None
    epics: list[str]
    rank: int | None
    position: int
    sweep_position: int
    status: str
    status_reason: str
    next_milestone: str | None
    final: str | None
    plan_next: str | None
    plan_final: str | None
    overdue: list[str]
    complete: bool
    completed_at: str | None
    completed_by: str | None
    updated_at: str | None
    open: list[BoardIssue]
    n_open: int
    n_closed: int
    n_in_progress: int
    #: Unreadable epics and named issues.
    unread_epics: list[str]
    closed_recent: list[BoardIssue]
    review: bool
    review_reason: str | None


class Board(TypedDict):
    declared: bool
    published_to: str | None
    published_at: str | None
    publish_error: str | None
    store: str | None
    writable: bool
    error: str | None
    read_at: str | None
    unread_epics: list[str]
    active: list[BoardRow]
    archive: list[BoardRow]
    sweep_order: list[str]
    sweep_rule: str
    review: list[str]


def parse(planner_raw: object) -> Declaration | None:
    """`planner.board` as a Declaration, or None when the board is not on."""
    if not isinstance(planner_raw, Mapping):
        return None
    raw = cast(Mapping[str, object], planner_raw).get("board")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise BoardConfigError("planner.board must be a mapping with `store`")
    spec = cast(Mapping[str, object], raw)
    unknown = sorted(set(map(str, spec)) - {"store", "publish"})
    if unknown:
        raise BoardConfigError(f"planner.board: unknown key(s) {', '.join(unknown)}")
    m = _STORE.match(str(spec.get("store") or ""))
    if m is None:
        raise BoardConfigError("planner.board.store must be s3://<bucket>/<key>.json")
    publish: tuple[str, str] | None = None
    if spec.get("publish") is not None:
        pm = _STORE.match(str(spec.get("publish") or ""))
        if pm is None:
            raise BoardConfigError("planner.board.publish must be s3://<bucket>/<key>.json")
        if (pm["bucket"], pm["key"]) == (m["bucket"], m["key"]):
            raise BoardConfigError("planner.board.publish must not be the store itself")
        publish = (pm["bucket"], pm["key"])
    return Declaration(bucket=m["bucket"], key=m["key"], publish=publish)


# ------------------------------------------------------------------ store --

def _s3_get(bucket: str, key: str) -> bytes | None:
    from . import ticks  # one S3 read path for both stores

    return ticks._s3_get(bucket, key)  # pyright: ignore[reportPrivateUsage]


def _s3_put(bucket: str, key: str, body: bytes) -> None:
    from . import ticks

    ticks._s3_put(bucket, key, body)  # pyright: ignore[reportPrivateUsage]


def _entries(body: bytes | None, decl: Declaration) -> dict[str, Entry]:
    if body is None:
        return {}
    doc = cast(object, json.loads(body.decode("utf-8")))
    projects = cast(Mapping[str, object], doc).get("projects") if isinstance(doc, Mapping) else None
    if not isinstance(projects, Mapping):
        raise ValueError(f"{decl.uri} has no `projects` mapping")
    return {str(k): dict(cast(Mapping[str, object], v))
            for k, v in cast(Mapping[str, object], projects).items() if isinstance(v, Mapping)}


def read(decl: Declaration | None, now: str,
         getter: Getter | None = None) -> tuple[Store | None, list[AdapterResult]]:
    """The board's entries. An unreadable store is a FAILED source and the
    board renders read-only with the reason."""
    if decl is None:
        return None, []
    try:
        found = Store(decl, _entries((getter or _s3_get)(decl.bucket, decl.key), decl),
                      read_at=now)
        unavailable: tuple[str, ...] = ()
    except Exception as exc:  # noqa: BLE001 - shown as a failed source
        found = Store(decl, read_at=now, error=f"{type(exc).__name__}: {str(exc)[:160]}")
        unavailable = (found.error or "",)
    status = AdapterStatus.FAILED if unavailable else AdapterStatus.OK
    return found, [AdapterResult(name=SOURCE_NAME, status=status,
                                 claim_class=ClaimClass.OBSERVATION, fetched_at=now,
                                 unavailable=unavailable)]


def attach(index: object, found: Store | None, epics: Epics | None = None) -> None:
    setattr(index, _ATTR, found)
    setattr(index, _EPICS_ATTR, epics or Epics())


def store(index: object) -> Store | None:
    found = cast(object, getattr(index, _ATTR, None))
    return found if isinstance(found, Store) else None


def epics(index: object) -> Epics:
    found = cast(object, getattr(index, _EPICS_ATTR, None))
    return found if isinstance(found, Epics) else Epics()


# ------------------------------------------------------------- evaluation --

def _date_or_none(value: object) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None


def _rank(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _active_order(project_ids: list[str], entries: Mapping[str, Entry]) -> list[str]:
    """Active (not complete) project ids: ranked ones by rank, then the rest
    in declared order."""
    def key(slot_pid: tuple[int, str]) -> tuple[bool, int, int]:
        r = _rank((entries.get(slot_pid[1]) or {}).get("rank"))
        return (r is None, r or 0, slot_pid[0])

    active = [(slot, pid) for slot, pid in enumerate(project_ids)
              if not (entries.get(pid) or {}).get("complete")]
    return [pid for _, pid in sorted(active, key=key)]


def _issue(ref: str, title: str | None, url: str | None, labels: list[str],
           blocked_by: list[str], blocker_labels: tuple[str, ...]) -> BoardIssue:
    gates = [lab for lab in labels if lab in blocker_labels]
    return BoardIssue(
        ref=ref, title=title, url=url, in_progress=IN_PROGRESS_LABEL in labels,
        blocker=(f"gated: {', '.join(gates)}" if gates
                 else (f"blocked by {', '.join(blocked_by)}" if blocked_by else None)))


def _named_refs(epic_refs: list[str], issues: list[str],
                milestones: list[Mapping[str, object]]) -> list[str]:
    named = list(issues)
    for m in milestones:
        for item in cast(list[Mapping[str, object]], m.get("items") or []):
            ref = str(item.get("ref") or "")
            if ref and not ref.startswith("predicate:") and ref not in epic_refs:
                named.append(ref)
    return list(dict.fromkeys(named))


def _recent(closed_at: str | None, since: datetime) -> bool:
    if not closed_at:
        return False
    try:
        at = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)) >= since


def _project_issues(index: object, epic_refs: list[str], named: list[str],
                    read: Epics, blocker_labels: tuple[str, ...], since: datetime
                    ) -> tuple[list[BoardIssue], int, list[str], list[BoardIssue]]:
    from . import planner

    open_items: dict[str, BoardIssue] = {}
    closed: set[str] = set()
    recent: dict[str, BoardIssue] = {}
    unread_named: list[str] = []
    for epic in epic_refs:
        for row in read.rows.get(epic, []):
            if row["closed"]:
                closed.add(row["ref"])
                if _recent(row["closed_at"], since):
                    recent[row["ref"]] = _issue(row["ref"], row["title"], row["url"],
                                                [], [], blocker_labels)
            else:
                open_items.setdefault(row["ref"], _issue(
                    row["ref"], row["title"], row["url"], row["labels"], [], blocker_labels))
    for ref in named:
        item = cast(Mapping[str, object],
                    planner._item(index, ref, blocker_labels))  # pyright: ignore[reportPrivateUsage]
        if not item.get("readable"):
            unread_named.append(ref)
            continue
        if item.get("closed"):
            closed.add(ref)
            if _recent(_opt_str(item.get("closed_at")), since):
                recent.setdefault(ref, _issue(ref, _opt_str(item.get("title")),
                                              _opt_str(item.get("url")), [], [], blocker_labels))
        elif ref not in open_items:
            open_items[ref] = _issue(
                ref, _opt_str(item.get("title")), _opt_str(item.get("url")),
                [str(x) for x in cast(list[object], item.get("labels") or [])],
                [str(x) for x in cast(list[object], item.get("blocked_by") or [])],
                blocker_labels)
    ordered = sorted(open_items.values(),
                     key=lambda i: (not i["in_progress"], i["blocker"] is None))
    closed -= set(open_items)
    unread = [e for e in epic_refs if e not in read.rows]
    return (ordered, len(closed), unread + [r for r in unread_named if r not in unread],
            [i for r, i in recent.items() if r not in open_items])


def _status(milestones: list[Mapping[str, object]], late: list[str]) -> tuple[str, str]:
    """The row's schedule status and why: the worst unmet milestone state on
    the calendar, and any board date already past."""
    from . import planner

    states = {str(m.get("state")): str(m.get("title")) for m in milestones
              if m.get("state") != planner.MET}
    if planner.OFF_TRACK in states or late:
        why = ([f"milestone {states[planner.OFF_TRACK]!r} off track"]
               if planner.OFF_TRACK in states else []) + [f"{x} date passed" for x in late]
        return OFF_TRACK_WORD, "; ".join(why)
    if planner.THREATENED in states:
        return THREATENED_WORD, f"milestone {states[planner.THREATENED]!r} threatened"
    if planner.ON_TRACK in states or planner.UNREPORTED in states:
        return ON_TRACK_WORD, "dated milestones on track"
    return NO_DATES_WORD, "no open dated milestone"


def _review(milestones: list[Mapping[str, object]], n_open: int, n_closed: int,
            unread: list[str]) -> str | None:
    """Why an active project looks finished and needs Brian's review before
    he ticks done (Brian 2026-11-09 23:04Z), or ``None``. Evidence only:
    every dated milestone met and no open issue left on its epics or named
    refs. An unreadable epic or named issue is never evidence of done."""
    from . import planner

    if unread or n_open:
        return None
    met = sum(1 for m in milestones if m.get("state") == planner.MET)
    if milestones and met == len(milestones):
        return f"all {met} milestones met, no open issues ({n_closed} closed)"
    if not milestones and n_closed:
        return f"no open issues left ({n_closed} closed)"
    return None


def _sweep_order(active: list[BoardRow]) -> list[str]:
    def key(r: BoardRow) -> tuple[int, int]:
        lift = (OFF_TRACK_LIFT if r["status"] == OFF_TRACK_WORD else
                THREATENED_LIFT if r["status"] == THREATENED_WORD else 0)
        return (r["position"] - lift, r["position"])

    return [r["id"] for r in sorted(active, key=key)]


SWEEP_RULE = (f"rank, moved up {OFF_TRACK_LIFT} places when off track or past a "
              f"board date and {THREATENED_LIFT} when threatened")


def _plan_dates(milestones: list[Mapping[str, object]]) -> tuple[str | None, str | None]:
    """The calendar's next unmet target and last target, shown as hints."""
    from . import planner

    pending = sorted(str(m["target"]) for m in milestones if m.get("state") != planner.MET)
    finals = sorted(str(m["target"]) for m in milestones)
    return (pending[0] if pending else None), (finals[-1] if finals else None)


def evaluate(index: object, today: date | None = None,
             now: datetime | None = None) -> Board:
    """The board: active projects in rank order, then the archive. Pure over
    the built index and the store read with it."""
    from . import planner

    now = now or datetime.now(timezone.utc)
    today = today or now.date()
    since = now - timedelta(hours=RECENT_HOURS)
    plan = planner.declared(index)
    projects = cast(list[Mapping[str, object]], planner.evaluate(index, now=today))
    found = store(index)
    entries: Mapping[str, Entry] = found.entries if found else {}
    read = epics(index)
    rows: dict[str, BoardRow] = {}
    ids: list[str] = []
    for p in projects:
        pid = str(p["id"])
        ids.append(pid)
        e = entries.get(pid) or {}
        milestones = cast(list[Mapping[str, object]], p.get("milestones") or [])
        epic_refs = [str(x) for x in cast(list[object], p.get("epics") or [])]
        issues = [str(x) for x in cast(list[object], p.get("issues") or [])]
        plan_next, plan_final = _plan_dates(milestones)
        nxt, fin = _date_or_none(e.get("next_milestone")), _date_or_none(e.get("final"))
        complete = bool(e.get("complete"))
        late = [label for label, d in (("next milestone", nxt), ("final phase", fin))
                if d and not complete and d < today.isoformat()]
        open_items, n_closed, unread, recent = _project_issues(
            index, epic_refs, _named_refs(epic_refs, issues, milestones), read,
            plan.blocker_labels, since)
        status, status_reason = _status(milestones, late)
        review = None if complete else _review(milestones, len(open_items), n_closed, unread)
        rows[pid] = BoardRow(
            id=pid, title=str(p["title"]), url=str(p["url"]),
            tracker=_opt_str(p.get("tracker")), owner=_opt_str(p.get("owner")),
            epics=epic_refs, rank=_rank(e.get("rank")), position=0, sweep_position=0,
            status=status, status_reason=status_reason,
            next_milestone=nxt, final=fin, plan_next=plan_next, plan_final=plan_final,
            overdue=late, complete=complete,
            completed_at=_opt_str(e.get("completed_at")) if complete else None,
            completed_by=_opt_str(e.get("by")) if complete else None,
            updated_at=_opt_str(e.get("updated_at")),
            open=open_items, n_open=len(open_items), n_closed=n_closed,
            n_in_progress=sum(1 for i in open_items if i["in_progress"]),
            unread_epics=unread, closed_recent=recent,
            review=review is not None, review_reason=review,
        )
    active = [rows[i] for i in _active_order(ids, entries)]
    for pos, r in enumerate(active, start=1):
        r["position"] = pos
    sweep = _sweep_order(active)
    for r in active:
        r["sweep_position"] = sweep.index(r["id"]) + 1
    archive = sorted((r for r in rows.values() if r["complete"]),
                     key=lambda r: r["completed_at"] or "", reverse=True)
    return Board(
        declared=found is not None,
        published_to=(f"s3://{found.declaration.publish[0]}/{found.declaration.publish[1]}"
                      if found and found.declaration.publish else None),
        published_at=found.published_at if found else None,
        publish_error=found.publish_error if found else None,
        store=found.declaration.uri if found else None,
        writable=found is not None and found.error is None,
        error=found.error if found else None,
        read_at=found.read_at if found else None,
        unread_epics=list(read.unread),
        active=active, archive=archive, sweep_order=sweep, sweep_rule=SWEEP_RULE,
        review=[r["id"] for r in active if r["review"]],
    )


# ------------------------------------------------------------------- write --

_LOCK = threading.Lock()


def _form_date(value: str | None, name: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise SaveRefused(f"{name} must be a date YYYY-MM-DD, not {value!r}") from None
    if not 2020 <= d.year <= 2100:
        raise SaveRefused(f"{name} {value} is out of range")
    return d.isoformat()


def save(index: object, project_id: str, form: Mapping[str, str], actor: str | None,
         now: str, getter: Getter | None = None,
         putter: Putter | None = None) -> Entry:
    """Save one row's inputs. Re-reads the store, applies the edit, renumbers
    the active ranks 1..n, writes the whole object, and reflects it in the
    index the page reads. Returns the saved entry."""
    from . import planner

    found = store(index)
    if found is None:
        raise SaveRefused("the planning board is not configured on this console", 404)
    ids = [p.id for p in planner.declared(index).projects]
    if not PROJECT_ID.match(project_id) or project_id not in ids:
        raise SaveRefused(f"{project_id!r} is not a declared planner project", 404)
    raw_rank = (form.get("rank") or "").strip()
    rank: int | None = None
    if raw_rank:
        if not raw_rank.isdigit() or not 1 <= int(raw_rank) <= 999:
            raise SaveRefused(f"rank must be a whole number from 1, not {raw_rank!r}")
        rank = int(raw_rank)
    nxt = _form_date(form.get("next_milestone"), "next milestone")
    fin = _form_date(form.get("final"), "final phase date")
    complete = form.get("complete", "0") not in {"", "0"}
    decl = found.declaration
    with _LOCK:
        try:
            entries = _entries((getter or _s3_get)(decl.bucket, decl.key), decl)
        except Exception as exc:  # noqa: BLE001 - nothing was written
            raise SaveRefused(f"could not read {decl.uri}: {str(exc)[:160]}", 502) from None
        before: Entry = dict(entries.get(project_id) or {})
        prior = _active_order(ids, entries)
        entry: Entry = {**before, "next_milestone": nxt, "final": fin,
                        "updated_at": now, "updated_by": actor}
        if complete and not before.get("complete"):
            entry.update(complete=True, completed_at=now, by=actor)
        elif not complete:
            entry.update(complete=False, completed_at=None, by=None)
        entries[project_id] = entry
        order = [i for i in prior if i != project_id]
        if not complete:
            if rank is not None:
                where = rank - 1
            elif project_id in prior:
                where = prior.index(project_id)  # unchanged rank: stays put
            else:
                where = len(order)  # reopened with no rank: the bottom
            order.insert(min(where, len(order)), project_id)
        for pid in ids:
            e = entries.setdefault(pid, {})
            e["rank"] = order.index(pid) + 1 if pid in order else None
        doc: dict[str, object] = {"schema_version": SCHEMA_VERSION, "updated_at": now,
                                  "projects": entries}
        try:
            from ..render.json import dumps

            (putter or _s3_put)(decl.bucket, decl.key, dumps(doc).encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - nothing was written
            raise SaveRefused(f"could not save {decl.uri}: {str(exc)[:160]}", 502) from None
        found.entries, found.read_at, found.error = entries, now, None
    return entries[project_id]


#: The whole-table form (Brian 2026-11-09 23:02Z: one Save for the table, not
#: one per row). Each shown row posts ``row.<id>`` (its shown position) beside
#: ``rank.<id>``, ``next_milestone.<id>``, ``final.<id>`` and, when ticked,
#: ``complete.<id>``.
ROW_FIELDS = ("row", "rank", "next_milestone", "final", "complete")


def _rank_field(raw: str | None, title: str) -> int | None:
    value = (raw or "").strip()
    if not value:
        return None
    if not value.isdigit() or not 1 <= int(value) <= 999:
        raise SaveRefused(f"rank for {title} must be a whole number from 1, not {value!r}")
    return int(value)


def save_all(index: object, form: Mapping[str, str], actor: str | None, now: str,
             getter: Getter | None = None, putter: Putter | None = None) -> list[str]:
    """Save every row the table posted, in one write. All or nothing: any
    bad field refuses the whole save and nothing is written.

    Ranks are positions. Rows whose rank was not changed keep their order;
    each changed row is then put at the place it asks for (lowest rank
    first), so typing "2" on a row moves it to second and shifts the rest. A
    blank rank keeps the row where it was. Rows ticked complete leave the order
    (to the archive), and the rest are renumbered 1..n. Returns the ids
    whose entry changed."""
    from . import planner

    found = store(index)
    if found is None:
        raise SaveRefused("the planning board is not configured on this console", 404)
    ids = [p.id for p in planner.declared(index).projects]
    titles = {p.id: p.title for p in planner.declared(index).projects}
    posted: dict[str, dict[str, str]] = {}
    for name, value in form.items():
        field, _, pid = name.partition(".")
        if field not in ROW_FIELDS or not pid:
            continue
        if not PROJECT_ID.match(pid) or pid not in ids:
            raise SaveRefused(f"{pid!r} is not a declared planner project", 404)
        posted.setdefault(pid, {})[field] = value
    rows = {pid: f for pid, f in posted.items() if "row" in f}
    if not rows:
        raise SaveRefused("nothing to save: the form carried no board rows")
    parsed: dict[str, tuple[int | None, int | None, str | None, str | None, bool]] = {}
    for pid, f in rows.items():
        title = titles.get(pid, pid)
        shown = _rank_field(f.get("row"), title)
        parsed[pid] = (_rank_field(f.get("rank"), title), shown,
                       _form_date(f.get("next_milestone"), f"next milestone for {title}"),
                       _form_date(f.get("final"), f"final phase date for {title}"),
                       f.get("complete", "0") not in {"", "0"})
    decl = found.declaration
    with _LOCK:
        try:
            entries = _entries((getter or _s3_get)(decl.bucket, decl.key), decl)
        except Exception as exc:  # noqa: BLE001 - nothing was written
            raise SaveRefused(f"could not read {decl.uri}: {str(exc)[:160]}", 502) from None
        prior = _active_order(ids, entries)
        changed: list[str] = []
        for pid, (_, _, nxt, fin, complete) in parsed.items():
            before: Entry = dict(entries.get(pid) or {})
            entry: Entry = {**before, "next_milestone": nxt, "final": fin}
            if complete and not before.get("complete"):
                entry.update(complete=True, completed_at=now, by=actor)
            elif not complete and before.get("complete"):
                entry.update(complete=False, completed_at=None, by=None)
            if any(entry.get(k) != before.get(k)
                   for k in ("next_milestone", "final", "complete")):
                entry.update(updated_at=now, updated_by=actor)
                changed.append(pid)
            entries[pid] = entry

        keep = [pid for pid in prior if not (entries.get(pid) or {}).get("complete")]
        moved: list[tuple[int, int, str]] = []
        for slot, pid in enumerate(keep):
            if pid in parsed:
                rank, shown = parsed[pid][0], parsed[pid][1]
                if rank is not None and rank != (shown or slot + 1):
                    moved.append((rank, slot, pid))
        order = [pid for pid in keep if pid not in {m[2] for m in moved}]
        last: tuple[int, int] | None = None  # (rank, index) of the last insert
        for rank, _, pid in sorted(moved):
            at = last[1] + 1 if last and last[0] == rank else rank - 1
            at = min(at, len(order))
            order.insert(at, pid)
            last = (rank, at)
        order += [pid for pid in ids if pid not in prior
                  and not (entries.get(pid) or {}).get("complete")]  # reopened: bottom
        for pid in ids:
            e = entries.setdefault(pid, {})
            rank = order.index(pid) + 1 if pid in order else None
            if e.get("rank") != rank and pid not in changed and pid in parsed:
                changed.append(pid)
            e["rank"] = rank
        doc: dict[str, object] = {"schema_version": SCHEMA_VERSION, "updated_at": now,
                                  "projects": entries}
        try:
            from ..render.json import dumps

            (putter or _s3_put)(decl.bucket, decl.key, dumps(doc).encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - nothing was written
            raise SaveRefused(f"could not save {decl.uri}: {str(exc)[:160]}", 502) from None
        found.entries, found.read_at, found.error = entries, now, None
    return changed


def publish(index: object, now: str, putter: Putter | None = None) -> None:
    """Write the whole /planner.json document to the declared ``publish``
    object, once per build. Never fatal: a failure is recorded on the store
    and shown on the page, and the console keeps serving."""
    found = store(index)
    if found is None or found.declaration.publish is None:
        return
    bucket, key = found.declaration.publish
    try:
        from ..render.json import dumps, payload
        from ..server.router import resolve

        doc = payload(index, resolve("/planner", ""))  # pyright: ignore[reportArgumentType]
        doc["published_at"] = now
        (putter or _s3_put)(bucket, key, dumps(doc).encode("utf-8"))
        found.published_at, found.publish_error = now, None
    except Exception as exc:  # noqa: BLE001 - recorded, shown on the page
        found.publish_error = f"{type(exc).__name__}: {str(exc)[:160]}"
