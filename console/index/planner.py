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
alone (§5.7), and there is no project-level roll-up state — a project row
shows each milestone's own state side by side (§4.3: no aggregate green light).
On the calendar a project's colour is its IDENTITY, not a verdict: bars wear
the project's colour, and the state sits on the target diamond with its word.

``MET``         every required item closed (and any bound predicate EXITED).
``UNREPORTED``  a required item could not be read, or nothing was declared to
                measure — never quietly on track (§5.5).
``OFF_TRACK``   the target passed unmet, or the work is behind the calendar by
                more than ``off_track_gap``: replan it.
``THREATENED``  behind by more than ``threatened_gap`` (pace is judged only
                with at least ``min_pace_items`` required items), or an open required
                item is gated on a person or blocked by another issue within
                ``blocker_window_days`` of the target.
``ON_TRACK``    none of the above.

**Alert updates** (Brian 2026-10-14: "lets make sure our alert updates are
reflected there as well"). A project may declare `alerts:`, a list of entity
SELECTORS over rows other adapters already mint (the crucible page bus, a
daily report's history row, ...): every entity whose facets equal the
declared `facets` (and, optionally, whose `kind` and declared field values
match) is one alert update, and the project renders its newest `limit` with
the time, a one-line text and a link. The tag that puts an alert on a project
is the selector in config, never inferred from the text, and the planner reads
no source of its own: an alert is visible here exactly when the adapter that
records it is healthy, and a selector that matches nothing says so by name
rather than rendering an empty list as "no alerts".

"Behind the calendar" is one comparison: the fraction of the window
``start -> target`` already elapsed, minus the fraction of required items
closed. It is deliberately linear and deliberately simple — a reader can redo
it from the two numbers the row renders, which a velocity forecast over a
handful of issues could not offer.
"""
from __future__ import annotations

import string
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

#: How many alert updates a project shows when its `alerts:` names no `limit`.
DEFAULT_ALERT_LIMIT = 5
#: A rendered alert line is a headline, not the message: the first line,
#: cut here. The whole record is one click away on its own row.
ALERT_TEXT_MAX = 240


class PlannerConfigError(ValueError):
    """A `planner:` declaration this build cannot evaluate. Raised by `parse`
    during `config.build_index`, so it fails the build naming the project and
    milestone rather than rendering a plan that silently answers nothing."""


@dataclass(frozen=True)
class Thresholds:
    threatened_gap: float = 0.15
    off_track_gap: float = 0.40
    blocker_window_days: int = 7
    #: Below this many required items the pace comparison is not made: one
    #: open issue reads 0% done on every day until it closes, so pace would
    #: turn every single-issue milestone red a third of the way in. Such a
    #: milestone is judged on its date and its blockers alone.
    min_pace_items: int = 3


@dataclass(frozen=True)
class Milestone:
    id: str
    title: str
    start: date
    target: date
    requires: tuple[str, ...] = ()
    predicate: str | None = None
    tracker: str | None = None
    #: False when every required item closes only AT the target by design (a
    #: gate read on the day): closure count then reads 0/N until that day on a
    #: perfect week, so pace says nothing and the milestone is judged on its
    #: date and blockers alone.
    pace: bool = True


@dataclass(frozen=True)
class AlertSource:
    """One declared selector over already-minted entities."""
    name: str
    facets: tuple[tuple[str, str], ...]
    kind: str | None = None
    #: `str.format` template over the entity's declared field names; a field
    #: it names that the row does not carry falls back to the row's label/id.
    text: str | None = None
    #: Declared field holding the instant the alert was sent or last observed;
    #: unset (or absent on the row) means the row's own `as_of`.
    at: str | None = None
    #: Declared field holding an http(s) link; unset or not a URL falls back
    #: to the row's evidence link when that is http(s), else its console page.
    link: str | None = None
    #: Declared field values a row must carry to count (e.g. `sent: true`).
    where: tuple[tuple[str, Any], ...] = ()
    #: Declared field values that drop a row (e.g. `synthetic: true` drills).
    exclude: tuple[tuple[str, Any], ...] = ()

    @property
    def selector(self) -> str:
        parts = ([f"kind={self.kind}"] if self.kind else []) + [
            f"{k}={v}" for k, v in self.facets] + [
            f"{k}={v}" for k, v in self.where] + [
            f"{k}!={v}" for k, v in self.exclude]
        return " ".join(parts)


@dataclass(frozen=True)
class Alerts:
    sources: tuple[AlertSource, ...]
    limit: int = DEFAULT_ALERT_LIMIT


@dataclass(frozen=True)
class Project:
    id: str
    title: str
    milestones: tuple[Milestone, ...]
    owner: str | None = None
    tracker: str | None = None
    alerts: Alerts | None = None


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


def _parse_alerts(raw: Any, pid: str) -> Alerts | None:
    if raw is None:
        return None
    where = f"planner project {pid!r} alerts"
    if not isinstance(raw, Mapping):
        raise PlannerConfigError(f"{where}: must be a mapping with `sources:`")
    limit = raw.get("limit", DEFAULT_ALERT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise PlannerConfigError(f"{where}: `limit` must be an integer 1..50")
    from ..model.kinds import Kind

    sources: list[AlertSource] = []
    names: set[str] = set()
    for s_ord, src in enumerate(raw.get("sources") or [], start=1):
        here = f"{where} source #{s_ord}"
        if not isinstance(src, Mapping):
            raise PlannerConfigError(f"{here}: must be a mapping")
        name = _text(src, "name", here)
        here = f"{where} source {name!r}"
        if name in names:
            raise PlannerConfigError(f"{here}: declared twice")
        names.add(name)
        facets = src.get("facets")
        if not isinstance(facets, Mapping) or not facets:
            # A selector with no facet would claim every row in the index.
            raise PlannerConfigError(
                f"{here}: `facets` must name at least one facet the alert rows carry")
        kind = str(src["kind"]).strip() if src.get("kind") else None
        if kind is not None:
            try:
                Kind(kind)
            except ValueError:
                raise PlannerConfigError(f"{here}: unknown `kind` {kind!r}") from None
        match = src.get("where") or {}
        drop = src.get("exclude") or {}
        if not isinstance(match, Mapping) or not isinstance(drop, Mapping):
            raise PlannerConfigError(
                f"{here}: `where` and `exclude` must map field names to values")
        text = str(src["text"]) if src.get("text") else None
        if text is not None:
            try:
                list(string.Formatter().parse(text))
            except ValueError as exc:
                raise PlannerConfigError(f"{here}: `text` is not a template: {exc}") from None
        sources.append(AlertSource(
            name=name,
            facets=tuple((str(k), str(v)) for k, v in facets.items()),
            kind=kind, text=text,
            at=(str(src["at"]) if src.get("at") else None),
            link=(str(src["link"]) if src.get("link") else None),
            where=tuple((str(k), v) for k, v in match.items()),
            exclude=tuple((str(k), v) for k, v in drop.items()),
        ))
    if not sources:
        raise PlannerConfigError(f"{where}: `sources` must declare at least one selector")
    return Alerts(sources=tuple(sources), limit=limit)


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
        min_pace_items=int(t.get("min_pace_items", Thresholds.min_pace_items)),
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
            pace = m.get("pace", True)
            if not isinstance(pace, bool):
                raise PlannerConfigError(f"{where}: `pace` must be true or false")
            milestones.append(Milestone(
                id=mid, title=_text(m, "title", where), start=start, target=target,
                requires=requires, predicate=predicate,
                tracker=(str(m["tracker"]) if m.get("tracker") else None),
                pace=pace,
            ))
        milestones.sort(key=lambda ms: (ms.target, ms.id))
        projects.append(Project(
            id=pid, title=_text(p, "title", f"planner project {pid!r}"),
            milestones=tuple(milestones),
            owner=(str(p["owner"]) if p.get("owner") else None),
            tracker=(str(p["tracker"]) if p.get("tracker") else None),
            alerts=_parse_alerts(p.get("alerts"), pid),
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
    paced = m.pace and total >= thresholds.min_pace_items
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
    elif paced and elapsed - done > thresholds.off_track_gap:
        state = OFF_TRACK
        reason = f"{progress}: more than {_pct(thresholds.off_track_gap)} behind, replan"
    elif paced and elapsed - done > thresholds.threatened_gap:
        state = THREATENED
        reason = f"{progress}: more than {_pct(thresholds.threatened_gap)} behind"
    elif blockers and days_left <= thresholds.blocker_window_days:
        state = THREATENED
        reason = (f"{len(blockers)} required item{'s' if len(blockers) != 1 else ''} blocked "
                  f"({'; '.join(b['ref'] + ' ' + b['blocker'] for b in blockers)}) "
                  f"with {days_left} days to target")
    elif not m.pace:
        state = ON_TRACK
        reason = (f"{progress}; its items close at the target by design, so it is "
                  f"judged on its date and blockers, not pace")
    elif total < thresholds.min_pace_items:
        state = ON_TRACK
        reason = (f"{progress}; with under {thresholds.min_pace_items} required "
                  f"items, judged on its date and blockers, not pace")
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


def _field_value(ent: Any, name: str) -> Any:
    spec = (ent.detail.get("fields") or {}).get(name)
    return spec.get("value") if isinstance(spec, Mapping) else spec


def _is_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("https://", "http://"))


def _headline(value: str) -> str:
    line = next((ln.strip() for ln in value.splitlines() if ln.strip()), "")
    return line if len(line) <= ALERT_TEXT_MAX else line[:ALERT_TEXT_MAX - 1] + "…"


def _alert_text(ent: Any, src: AlertSource) -> str:
    fallback = str(ent.detail.get("label") or ent.id)
    if not src.text:
        return _headline(fallback)
    names = [f for _, f, _, _ in string.Formatter().parse(src.text) if f]
    values = {n: _field_value(ent, n) for n in names}
    if any(v is None for v in values.values()):
        return _headline(fallback)
    try:
        return _headline(src.text.format(**values)) or _headline(fallback)
    except (KeyError, IndexError, ValueError, AttributeError):
        return _headline(fallback)


def _instant(value: Any) -> tuple[datetime | None, str | None]:
    if value is None:
        return None, None
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, raw or None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed, parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _matches(ent: Any, src: AlertSource) -> bool:
    if src.kind and ent.kind.value != src.kind:
        return False
    facets = ent.facets or {}
    if any(facets.get(k) != v for k, v in src.facets):
        return False
    if any(_field_value(ent, k) == v for k, v in src.exclude):
        return False
    return all(_field_value(ent, k) == v for k, v in src.where)


def evaluate_alerts(index: Any, alerts: Alerts | None) -> dict[str, Any]:
    """A project's newest alert updates across its declared selectors, newest
    first. Pure over the built index: the same rows give the same list."""
    if alerts is None:
        return {"declared": False, "limit": 0, "sources": [], "items": []}
    rows: list[tuple[datetime | None, str, dict[str, Any]]] = []
    sources = []
    entities = list(index.all())
    for src in alerts.sources:
        matched = 0
        for ent in entities:
            if not _matches(ent, src):
                continue
            matched += 1
            declared_at = _field_value(ent, src.at) if src.at else None
            when, at = _instant(declared_at if declared_at is not None
                                else ent.provenance.as_of)
            link = _field_value(ent, src.link) if src.link else None
            if not _is_url(link):
                link = ent.provenance.evidence if _is_url(ent.provenance.evidence) else ent.route
            rows.append((when, ent.id, {
                "source": src.name, "at": at, "text": _alert_text(ent, src),
                "url": link, "entity": ent.id, "entity_url": ent.route,
                "state": ent.state_value,
            }))
        sources.append({"name": src.name, "selector": src.selector, "matched": matched})
    # Newest first; an undatable row after every dated one. Equal instants
    # (a replay re-observes many incidents in one pass) break on the id,
    # descending, so the order is the same on every request and dated ids
    # list their newest day first.
    floor = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda r: (r[0] is not None, r[0] or floor, r[1]), reverse=True)
    return {"declared": True, "limit": alerts.limit, "sources": sources,
            "items": [r[2] for r in rows[:alerts.limit]]}


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
    for slot, p in enumerate(plan.projects):
        milestones = []
        for m in p.milestones:
            items = [_item(index, r, plan.blocker_labels) for r in m.requires]
            if m.predicate:
                items.append(_predicate_item(m.predicate, exits))
            milestones.append(evaluate_milestone(m, items, today, plan.thresholds))
        out.append({
            "id": p.id, "title": p.title, "owner": p.owner, "tracker": p.tracker,
            "url": f"/planner/{p.id}",
            # Declared position: the project's colour on the calendar follows
            # the project, so a project page draws it in the same colour.
            "slot": slot,
            # Counts per state, never one verdict (§4.3).
            "states": {s: sum(1 for m in milestones if m["state"] == s)
                       for s in SCHEDULE_STATES},
            "milestones": milestones,
            "alerts": evaluate_alerts(index, p.alerts),
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
