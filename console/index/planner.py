"""The delivery planner — "will each declared milestone land by its date, and
what is holding the ones that won't?" (`alpha-engine-config-I12152`).

No other pane answers that. The §4.4 milestone pane (`index/milestones.py`)
answers *has the declared milestone been met, and which clause is holding it*:
it is a predicate over the present. The planner is a SCHEDULE question: a
target date, the work that must close before it, and how far the work is
behind the calendar. It can bind a §4.4 predicate as one of its required
items, and that is the only relation between the two.

**Nothing about any particular project lives in this repo.** A project and its
milestones are a `planner:` config declaration; each milestone names a start,
a target and the tracker refs that must close (`requires`). The issue facts are
read by the git-host adapter's `fetch_refs` (`config.build_index` unions every
declared ref into one read), so the planner is a pure function of an already
built index plus its declarations, recomputed per query and never cached
(§5.6) — exactly like `milestones.evaluate`.

**Schedule state** is a CLOSED five-value vocabulary of its own, NOT §8.3's: a
milestone is not a component and is neither HEALTHY nor FAILED, the same
reasoning that gives the §4.4 pane MET/UNMET. Every value is rendered with the
reason that produced it in words, so colour never carries the distinction
alone (§5.7), and there is no project-level roll-up colour — a project row
shows each milestone's own state side by side (§4.3: no aggregate green light).

``MET``         every required item closed (and any bound predicate EXITED).
``UNREPORTED``  a required item could not be read, or nothing was declared to
                measure — never quietly on track (§5.5).
``OFF_TRACK``   the target passed unmet, or the work is behind the calendar by
                more than ``off_track_gap``: replan it.
``THREATENED``  behind by more than ``threatened_gap``, or an open required
                item is gated on a person or blocked by another issue within
                ``blocker_window_days`` of the target.
``ON_TRACK``    none of the above.

"Behind the calendar" is one comparison: the fraction of the window
``start -> target`` already elapsed, minus the fraction of required items
closed. It is deliberately linear and deliberately simple — a reader can redo
it from the two numbers the row renders, which a velocity forecast over a
handful of issues could not offer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

#: The question the planner panes answer, rendered on them (§4.4) and held here
#: so the pane registry, the HTML heading and the JSON cannot drift apart.
PLANNER_QUESTION = (
    "will each declared milestone land by its target date, and what is holding "
    "the ones that won't"
)

MET = "MET"
ON_TRACK = "ON_TRACK"
THREATENED = "THREATENED"
OFF_TRACK = "OFF_TRACK"
UNREPORTED = "UNREPORTED"

#: Closed, and ordered worst first — the order the project page lists them in.
SCHEDULE_STATES: tuple[str, ...] = (OFF_TRACK, UNREPORTED, THREATENED, ON_TRACK, MET)

#: Labels that put an open item in a person's hands rather than an agent's.
DEFAULT_BLOCKER_LABELS: tuple[str, ...] = (
    "gate:decision", "gate:operator", "gate:device", "blocked",
)

_ATTR = "_console_planner_declarations"


class PlannerConfigError(ValueError):
    """A `planner:` declaration this build cannot evaluate. Raised by `parse`
    during `config.build_index`, so it fails the build naming the project and
    milestone rather than rendering a plan that silently answers nothing."""


@dataclass(frozen=True)
class Thresholds:
    threatened_gap: float = 0.15
    off_track_gap: float = 0.40
    blocker_window_days: int = 7


@dataclass(frozen=True)
class Milestone:
    id: str
    title: str
    start: date
    target: date
    requires: tuple[str, ...] = ()
    predicate: str | None = None
    tracker: str | None = None


@dataclass(frozen=True)
class Project:
    id: str
    title: str
    milestones: tuple[Milestone, ...]
    owner: str | None = None
    tracker: str | None = None


@dataclass(frozen=True)
class Plan:
    projects: tuple[Project, ...]
    org: str | None = None
    thresholds: Thresholds = Thresholds()
    blocker_labels: tuple[str, ...] = DEFAULT_BLOCKER_LABELS
    fetch: Mapping[str, Any] = field(default_factory=dict)

    @property
    def refs(self) -> tuple[str, ...]:
        """Every tracker ref any milestone requires, once, in declared order."""
        seen: dict[str, None] = {}
        for p in self.projects:
            for m in p.milestones:
                for r in m.requires:
                    seen.setdefault(r, None)
        return tuple(seen)


EMPTY = Plan(projects=())


# ------------------------------------------------------------- declaration --

def _date(value: Any, where: str, key: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise PlannerConfigError(
            f"{where}: `{key}` must be a date YYYY-MM-DD, not {value!r}"
        ) from None


def _text(entry: Mapping[str, Any], key: str, where: str) -> str:
    value = str(entry.get(key) or "").strip()
    if not value:
        raise PlannerConfigError(f"{where}: `{key}` is required")
    return value


def parse(raw: Any) -> Plan:
    """Validate and freeze the `planner:` block. Raises on anything it could
    not evaluate honestly later."""
    if raw is None:
        return EMPTY
    if not isinstance(raw, Mapping):
        raise PlannerConfigError(
            f"`planner:` must be a mapping with `projects:`, not {type(raw).__name__}")
    from ..adapters.git_host import parse_issue_ref

    t = raw.get("thresholds") or {}
    thresholds = Thresholds(
        threatened_gap=float(t.get("threatened_gap", Thresholds.threatened_gap)),
        off_track_gap=float(t.get("off_track_gap", Thresholds.off_track_gap)),
        blocker_window_days=int(t.get("blocker_window_days",
                                      Thresholds.blocker_window_days)),
    )
    if not 0 <= thresholds.threatened_gap < thresholds.off_track_gap <= 1:
        raise PlannerConfigError(
            "planner thresholds: need 0 <= threatened_gap < off_track_gap <= 1")

    projects: list[Project] = []
    project_ids: set[str] = set()
    for p_ord, p in enumerate(raw.get("projects") or [], start=1):
        if not isinstance(p, Mapping):
            raise PlannerConfigError(f"planner project #{p_ord}: must be a mapping")
        pid = _text(p, "id", f"planner project #{p_ord}")
        if pid in project_ids:
            raise PlannerConfigError(f"planner project {pid!r}: declared twice")
        project_ids.add(pid)
        milestones: list[Milestone] = []
        m_ids: set[str] = set()
        for m_ord, m in enumerate(p.get("milestones") or [], start=1):
            where = f"planner project {pid!r} milestone #{m_ord}"
            if not isinstance(m, Mapping):
                raise PlannerConfigError(f"{where}: must be a mapping")
            mid = _text(m, "id", where)
            where = f"planner project {pid!r} milestone {mid!r}"
            if mid in m_ids:
                raise PlannerConfigError(f"{where}: declared twice")
            m_ids.add(mid)
            start = _date(m.get("start"), where, "start")
            target = _date(m.get("target"), where, "target")
            if target <= start:
                raise PlannerConfigError(f"{where}: `target` must be after `start`")
            requires = tuple(str(r).strip() for r in (m.get("requires") or []))
            bad = [r for r in requires if parse_issue_ref(r) is None]
            if bad:
                raise PlannerConfigError(
                    f"{where}: `requires` takes tracker refs `<repo>-I<N>`, not {bad}")
            predicate = str(m.get("predicate") or "").strip() or None
            milestones.append(Milestone(
                id=mid, title=_text(m, "title", where), start=start, target=target,
                requires=requires, predicate=predicate,
                tracker=(str(m["tracker"]) if m.get("tracker") else None),
            ))
        milestones.sort(key=lambda ms: (ms.target, ms.id))
        projects.append(Project(
            id=pid, title=_text(p, "title", f"planner project {pid!r}"),
            milestones=tuple(milestones),
            owner=(str(p["owner"]) if p.get("owner") else None),
            tracker=(str(p["tracker"]) if p.get("tracker") else None),
        ))
    labels = raw.get("blocker_labels")
    return Plan(
        projects=tuple(projects),
        org=(str(raw["org"]) if raw.get("org") else None),
        thresholds=thresholds,
        blocker_labels=(tuple(str(x) for x in labels) if labels is not None
                        else DEFAULT_BLOCKER_LABELS),
        fetch=dict(raw.get("fetch") or {}),
    )


def attach(index: Any, plan: Plan) -> None:
    """Hang the parsed plan off a built index — configuration, not graph, for
    the same reason `milestones.attach` gives."""
    setattr(index, _ATTR, plan)


def declared(index: Any) -> Plan:
    return getattr(index, _ATTR, EMPTY)


# ------------------------------------------------------------- evaluation --

def _today(now: datetime | date | None) -> date:
    if now is None:
        return datetime.now(timezone.utc).date()
    return now.date() if isinstance(now, datetime) else now


def _item(index: Any, ref: str, blocker_labels: Sequence[str]) -> dict[str, Any]:
    ent = index.entity(ref)
    if ent is None:
        return {"ref": ref, "readable": False, "closed": False, "blocker": None,
                "title": None, "url": None, "labels": [], "blocked_by": []}
    detail = ent.detail or {}
    closed = str(ent.state).lower() in {"closed", "resolved"}
    labels = list(detail.get("labels") or [])
    blocked_by = list(detail.get("blocked_by") or [])
    gating = [lab for lab in labels if lab in blocker_labels]
    blocker = None
    if not closed and gating:
        blocker = f"gated: {', '.join(gating)}"
    elif not closed and blocked_by:
        blocker = f"blocked by {', '.join(blocked_by)}"
    return {
        "ref": ref, "readable": True, "closed": closed, "blocker": blocker,
        "title": detail.get("title"), "url": ent.provenance.evidence,
        "state": str(ent.state), "labels": labels, "blocked_by": blocked_by,
        "closed_at": detail.get("closed_at"),
    }


def _predicate_item(predicate: str, exits: Mapping[str, str]) -> dict[str, Any]:
    state = exits.get(predicate)
    return {
        "ref": f"predicate:{predicate}", "readable": state is not None
        and state != "UNREPORTABLE", "closed": state == "EXITED", "blocker": None,
        "title": f"§4.4 milestone predicate {predicate}"
        + (f" ({state})" if state else " (not declared on this console)"),
        "url": f"/#milestone-{predicate}", "labels": [], "blocked_by": [],
        "predicate_state": state,
    }


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def evaluate_milestone(m: Milestone, items: list[dict[str, Any]], today: date,
                       thresholds: Thresholds) -> dict[str, Any]:
    """One milestone's schedule state and the reason, from its resolved items."""
    span = (m.target - m.start).days
    elapsed = min(1.0, max(0.0, (today - m.start).days / span))
    total = len(items)
    closed = sum(1 for i in items if i["closed"])
    done = closed / total if total else 0.0
    days_left = (m.target - today).days
    open_items = [i for i in items if not i["closed"]]
    blockers = [i for i in open_items if i["blocker"]]
    unread = [i["ref"] for i in items if not i["readable"]]
    window = f"{m.start.isoformat()} → {m.target.isoformat()}"
    progress = (f"{closed} of {total} items closed ({_pct(done)}) with "
                f"{_pct(elapsed)} of the window elapsed ({window})")

    if not total:
        state, reason = UNREPORTED, "nothing declared to measure: no required items"
    elif unread:
        state = UNREPORTED
        reason = f"cannot read {', '.join(unread)}; {progress}"
    elif not open_items:
        state, reason = MET, f"all {total} required items closed"
    elif days_left < 0:
        state = OFF_TRACK
        reason = (f"target {m.target.isoformat()} passed {-days_left} days ago "
                  f"with {len(open_items)} items open: replan")
    elif elapsed - done > thresholds.off_track_gap:
        state = OFF_TRACK
        reason = f"{progress}: more than {_pct(thresholds.off_track_gap)} behind, replan"
    elif elapsed - done > thresholds.threatened_gap:
        state = THREATENED
        reason = f"{progress}: more than {_pct(thresholds.threatened_gap)} behind"
    elif blockers and days_left <= thresholds.blocker_window_days:
        state = THREATENED
        reason = (f"{len(blockers)} required item{'s' if len(blockers) != 1 else ''} blocked "
                  f"({'; '.join(b['ref'] + ' ' + b['blocker'] for b in blockers)}) "
                  f"with {days_left} days to target")
    else:
        state, reason = ON_TRACK, progress
    return {
        "id": m.id, "title": m.title, "start": m.start.isoformat(),
        "target": m.target.isoformat(), "state": state, "reason": reason,
        "closed": closed, "of": total, "done_fraction": round(done, 3),
        "elapsed_fraction": round(elapsed, 3), "days_left": days_left,
        "blockers": [b["ref"] for b in blockers], "tracker": m.tracker,
        "predicate": m.predicate,
        # Open first, blocked first among the open — the order a reader acts in.
        "items": sorted(items, key=lambda i: (i["closed"], i["blocker"] is None)),
    }


def evaluate(index: Any, now: datetime | date | None = None,
             plan: Plan | None = None) -> list[dict[str, Any]]:
    """Every declared project with every milestone evaluated, in declared
    order. Pure: the same index, plan and date give the same answer."""
    plan = plan if plan is not None else declared(index)
    if not plan.projects:
        return []
    today = _today(now)
    exits: dict[str, str] = {}
    if any(m.predicate for p in plan.projects for m in p.milestones):
        # The §4.4 pane's own evaluation for this build, read rather than
        # recomputed, so the two panes can never disagree about one predicate.
        exits = {d["id"]: d["exit_state"] for d in index.landing_model().milestones}
    out: list[dict[str, Any]] = []
    for p in plan.projects:
        milestones = []
        for m in p.milestones:
            items = [_item(index, r, plan.blocker_labels) for r in m.requires]
            if m.predicate:
                items.append(_predicate_item(m.predicate, exits))
            milestones.append(evaluate_milestone(m, items, today, plan.thresholds))
        out.append({
            "id": p.id, "title": p.title, "owner": p.owner, "tracker": p.tracker,
            "url": f"/planner/{p.id}",
            # Counts per state, never one verdict (§4.3).
            "states": {s: sum(1 for m in milestones if m["state"] == s)
                       for s in SCHEDULE_STATES},
            "milestones": milestones,
        })
    return out


def project(index: Any, project_id: str,
            now: datetime | date | None = None) -> dict[str, Any] | None:
    return next((p for p in evaluate(index, now) if p["id"] == project_id), None)


def axis(projects: Iterable[Mapping[str, Any]]) -> tuple[date, date] | None:
    """The shared dated axis: earliest start to latest target across every
    declared milestone, or None when nothing is declared."""
    starts, targets = [], []
    for p in projects:
        for m in p["milestones"]:
            starts.append(date.fromisoformat(m["start"]))
            targets.append(date.fromisoformat(m["target"]))
    if not starts:
        return None
    return min(starts), max(targets)
