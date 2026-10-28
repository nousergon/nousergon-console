"""The machine-readable representation — same URL, same query (§3.8).

**The surface's readers include the fleet's own agents**, and `principles.md`
§1.1's end state has more of them, not fewer: the response plane, the grooms and
sweeps, the wind-down audit. A monitoring surface an agent cannot read forces
every agent to re-derive fleet state from raw sources, which is §2.4's second
inventory one layer up — the agent's picture and the operator's picture then
diverge, and they diverge **exactly when something is wrong**, because that is
when the derivations differ.

So this is not an API beside the UI. It is the *other rendering* of the same
resolved request against the same index:

    resolve(path, query)  ─┬─→  render.html.<view>_page(index, req)
                           └─→  render.json.payload(index, req)

`server/app.py` calls the resolver **once** and dispatches. The two
representations cannot drift in coverage, because there is one query and one
router; a route that exists serves both by construction, and a route that does
not exist 404s in both.

Every payload carries `schema_version`, so a consumer asserts rather than
sniffs.
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ..index.graph import Index
from ..index.numbers import artifact_observation_coverage as _artifact_observation_coverage
from ..index.numbers import claim_conflicts as _claim_conflicts
from ..index.numbers import not_healthy as _not_healthy
from ..model.entity import DOCUMENT_DETAIL, JOB_LABEL_DETAIL, LABEL_DETAIL, WINDOW_DETAIL, Edge, Entity
from ..model.kinds import STATE_FILTER, Kind, State
from ..model.fields import parse as parse_fields
from ..search.resolve import MATCHED_ON_LABEL, search
from ..server.router import Resolved

#: The wire version of this projection. Distinct from the adapter envelope's
#: version (`model/envelope.py`): one is what an adapter hands the index, the
#: other is what the index hands a consumer, and they change for different
#: reasons.
SCHEMA_VERSION = 1


def _wire_default(obj: Any) -> Any:
    """The ONE place a non-JSON Python value becomes a wire value.

    Adapters hand the index whatever their source held — a YAML registry row
    parses `created_at: 2026-08-10` as `datetime.date`, boto3 answers with
    `datetime` and `Decimal` — and `detail` passes through verbatim by design
    (§5.8: the console renders fields it has never seen). So the boundary that
    must know how to spell them is the serializer, once, for every emission
    site (`server/app.py`, `console index`, `emit.py`). Before this existed a
    single dated registry row raised inside the request handler and the
    surface answered nothing at all (config-I7432, 2026-08-17).

    Only types with one unambiguous textual form are accepted; anything else
    still raises, naming the type — a value the wire cannot spell is a
    contract violation, not something to `str()` quietly.
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (set, frozenset, tuple)):
        return sorted(obj, key=str) if isinstance(obj, (set, frozenset)) else list(obj)
    raise TypeError(
        f"{obj.__class__.__name__} is not representable on the console wire "
        f"(render/json.py::_wire_default)"
    )


def dumps(doc: Any) -> str:
    """Serialize a payload for the wire — every JSON emission site uses this
    and nothing calls `json.dumps` on console data directly (tests assert
    it), so the spelling of a date is the same on HTTP, on `console index`
    and in an emitted report.
    """
    return _json.dumps(doc, indent=2, sort_keys=True, default=_wire_default)


def aggregate(count: int, denominator: int | None) -> dict[str, int]:
    """The only JSON roll-up shape: a count without its population is invalid."""
    if denominator is None:
        raise ValueError("aggregate denominator is required (§5.3)")
    return {"count": count, "of": denominator}


def payload(index: Index, req: Resolved) -> dict[str, Any]:
    """The JSON body for a resolved request — the same query the HTML renders."""
    if req.view == "landing":
        doc = _landing(index)
    elif req.view == "list":
        doc = _list(index, req)
    elif req.view == "dashboards":
        model = index.landing_model()
        from ..index import pages as declared_pages

        doc = {"schema_version": SCHEMA_VERSION, "view": "dashboards",
               "pages": [{"id": p.id, "title": p.title, "url": p.url,
                          "question": p.question}
                         for p in declared_pages.declared(index)],
               "dashboards": model.dashboards,
               "milestones": [{"id": m["id"], "met": m["met"], "of": m["of"],
                               "unreported": m.get("unreported", 0)}
                              for m in model.milestones],
               "registries": index.registry_names(),
               # What each registry link says (I11805): read status, rows
               # shown, rows declared, not healthy. `registries` is unchanged.
               "registry_links": model.registry_links,
               "kinds": [{"kind": k.route, "url": f"/{k.route}",
                          "rows": len(index.of_kind(k))} for k in Kind]}
    elif req.view == "entity":
        ent = index.entity(req.entity_id or "")
        if ent is None:  # pragma: no cover - app.py 404s before reaching here
            raise KeyError(req.entity_id)
        doc = _entity_page(index, ent, all_runs=req.all_runs)
    elif req.view == "history":
        ent = index.entity(req.entity_id or "")
        if ent is None:
            raise KeyError(req.entity_id)
        from ..history import query as history_query
        doc = {"schema_version": SCHEMA_VERSION, "view": "history",
               "entity": entity(ent), "history": history_query(ent, req.window_hours or 24)}
    elif req.view == "search":
        doc = _search(index, req.query or "", all_runs=req.all_runs)
    elif req.view == "doctor":
        from ..diagnose import as_dict, doctor

        doc = {"schema_version": SCHEMA_VERSION, "view": "doctor",
               **as_dict(doctor(index, req.query or ""))}
    elif req.view == "planner":
        from ..index import planner

        projects = planner.evaluate(index)
        span = planner.axis(projects)
        doc = {"schema_version": SCHEMA_VERSION, "view": "planner",
               "question": planner.PLANNER_QUESTION,
               "states": list(planner.SCHEDULE_STATES),
               "axis": ({"start": span[0].isoformat(), "end": span[1].isoformat()}
                        if span else None),
               "projects": projects}
    elif req.view == "reports":
        from ..index import reports

        found = reports.feed(index, req.feed_id)
        if found is None and req.feed_id is not None:  # pragma: no cover - app.py 404s first
            raise KeyError(req.feed_id)
        doc = {"schema_version": SCHEMA_VERSION, "view": "reports",
               "question": reports.REPORTS_QUESTION,
               "feeds": [{"id": f.id, "title": f.title, "url": f.url}
                         for f in reports.declared(index)],
               "feed": reports.evaluate(index, found) if found else None}
    elif req.view == "planner-project":
        from ..index import planner

        found = planner.project(index, req.project_id or "")
        if found is None:  # pragma: no cover - app.py 404s before reaching here
            raise KeyError(req.project_id)
        doc = {"schema_version": SCHEMA_VERSION, "view": "planner-project",
               "question": planner.PLANNER_QUESTION,
               "states": list(planner.SCHEDULE_STATES), "project": found}
    elif req.view == "exceptions":
        from ..index import pages

        model = index.landing_model()
        view = model.exception_view
        rows = pages.by_priority(list(view.rows))
        doc = {"schema_version": SCHEMA_VERSION, "view": "exceptions",
               "question": pages.EXCEPTIONS_QUESTION,
               "tiers": list(pages.PRIORITY_TIERS), "total": len(model.exceptions),
               "rows": [{**entity(e, with_document=False),
                         "label": view.labels.get(e.id),
                         "priority": pages.PRIORITY_TIERS[pages.priority_rank(e)],
                         "runs": view.runs.get(e.id)} for e in rows]}
    elif req.view == "page":
        from ..index import pages

        found = pages.page(index, req.page_id)
        if found is None:  # pragma: no cover - app.py 404s before reaching here
            raise KeyError(req.page_id)
        doc = {"schema_version": SCHEMA_VERSION, "view": "page",
               "pages": [{"id": p.id, "title": p.title, "url": p.url}
                         for p in pages.declared(index)],
               "page": pages.evaluate(index, found, dict(req.params))}
    elif req.view == "registry":
        doc = _registry(index, req.registry_name or "", all_runs=req.all_runs)
    else:
        raise ValueError(f"no JSON representation for view {req.view!r}")
    # §5.9 on every payload, exactly as on every page: a consumer that trusts a
    # row without knowing how old the index is has the same problem a reader
    # does, and it is worse for them because nothing prompts them to ask.
    doc["index"] = index_freshness(index)
    return doc


def index_freshness(index: Index, now: datetime | None = None) -> dict[str, Any]:
    """The index's own as-of and every source's read (§5.9)."""
    info = index.build_info
    now = now or datetime.now(timezone.utc)
    return {
        "built_at": info.built_at or None,
        "refresh_seconds": info.refresh_seconds,
        "build_seconds": info.build_seconds,
        "cadence_overrun": info.cadence_overrun,
        "stale": info.is_stale(now),
        "staleness_basis": info.staleness_basis(),
        "age_seconds": info.age_seconds(now),
        "stale_since": info.stale_since,
        "last_error": info.last_error,
        # alpha-engine-config-I9052 deliverable 2: distinguishes the
        # deliberate bootstrap window (`Supervisor(defer_first_build=True)`
        # seeding a raw, adapter-less `Index()` while the first real build
        # runs) from a build that failed outright — a consumer can retry a
        # bootstrap read rather than treat its zeroed §9 numbers as fleet
        # state.
        "bootstrap": info.bootstrap,
        "sources": [
            {
                "name": a.name, "status": a.status, "fetched_at": a.fetched_at,
                "cadence_seconds": a.cadence_seconds,
                "elapsed_seconds": a.elapsed_seconds,
                "unavailable": list(a.unavailable),
            }
            for a in info.adapters
        ],
    }


# ---------------------------------------------------------------- views ----

def _landing(index: Index) -> dict[str, Any]:

    """The exception-first default (§4.3), and the numbers that grade it (§9).

    An agent asking "is anything wrong" gets the same answer, in the same
    order, as a human looking at the page — and, since alpha-engine-config-
    I10615, the same COMPUTATION `render.html.landing_page` reads: both read
    `index.landing_model()`, computed once per `Index` rather than once per
    request (`index/landing.py`).
    """
    from ..index import home as _home

    model = index.landing_model()
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "registry_pages": model.registries,
        "view": "landing",
        "exceptions": [entity(e) for e in model.exceptions],
        # §4.3's third element: what is waiting on Brian. Same query the HTML
        # renders (§3.8) — `Index.decision_queue()`, filtered to
        # `decision-queue-policy.md` §2's own labels, not every open Decision.
        "decision_queue": [entity(e) for e in model.queue],
        # console-policy.md §9 — the nine numbers, all published, all stating
        # their denominator inline (§5.3). A number this build could not
        # compute renders `state: N/A-NOT-IMPL` with the cycle it is expected
        # by (§11) — except §9.1/§9.2, which are never allowed that token:
        # §9.1 (population_completeness) signals uncomputable with
        # `of`/`ratio` both `None` (not wrapped in `aggregate()`, which would
        # reject a missing denominator outright), and §9.2 always has a real
        # denominator (the component population), so it never needs to.
        "numbers": model.numbers,
        # CN-4.3 (amended 2026-10-28): home opens with one health entry per
        # declared area (plus `unassigned`), then the declared headline tiles.
        # The same computation the HTML renders (§3.8).
        "health": _home.health(index, model.exceptions),
        "headline": _home.headline(index),
    }
    # console-policy.md §4.4's milestone pane, from the SAME assembly the HTML
    # renders (`render.html.milestones_section` reads the SAME `model.
    # milestones` list rather than calling `evaluate` a second time), so the
    # two representations of this URL cannot answer the predicate differently.
    #
    # The key is ABSENT when nothing is declared, rather than an empty list: a
    # deployment that declares no milestone has no milestone pane, and an empty
    # `milestones: []` reads to a consumer like a declared predicate with no
    # clauses — which `parse` refuses to build in the first place.
    # The same fold and names the HTML renders (§3.8, I11805). `exceptions`
    # above stays every row; this says which of them the page shows, what
    # each is called, and how many runs a shown row stands for.
    view = model.exception_view
    if view is not None:
        doc["exception_view"] = {
            "latest_per_job": [e.id for e in view.rows],
            "labels": view.labels,
            "runs": view.runs,
        }
    if model.queue_labels:
        doc["decision_queue_labels"] = model.queue_labels
    # The registry links the page lists, with what each says (I11805, §3.8).
    doc["registry_links"] = model.registry_links
    if model.milestones:
        doc["milestones"] = model.milestones
    # What the clause journal did on THIS build: the transitions it recorded,
    # the episodes still open, and — loudly — any failure to record at all. A
    # recorder that stopped working is a fact about the surface's own honesty
    # and must be readable by the agents that read this view, not only by a
    # human looking at the page (§3.8).
    if model.milestone_journal:
        doc["milestone_journal"] = model.milestone_journal
    return doc


#: §9.7 (surface liveness) is explicitly out of THIS issue's scope — it
#: belongs to the console's deploy work (nous-ergon-ops-I364) rather than to
#: the index/render layer this issue covers. §11's carve-out requires the
#: number be named and the cycle it is expected by stated, not silently
#: omitted — this is that statement.
def numbers(index: Index, exceptions: list[Entity], conflicts: list[Entity],
           gap: dict[str, Any], *,
           reachability: dict[str, Any] | None = None,
           population_completeness: dict[str, Any] | None = None) -> dict[str, Any]:
    """console-policy.md §9 — all nine numbers, one dict, one place they are
    assembled for both representations (§3.8: the HTML numbers section reads
    the same shape via `render.html.landing_numbers`).

    `reachability`/`population_completeness` are accepted pre-computed,
    exactly as `gap` already was: `index/landing.py::build` computes both once
    per index build and passes them through here rather than paying for
    `index.reachability()` (§9.3's own §3.1 traversal is the dominant per-page
    cost measured on the box — alpha-engine-config-I10615) a second time in
    the same pass. `None` (every other caller, including every test that
    calls this directly) computes them here exactly as before.
    """
    entities = index.all()
    return {
        # §9.1 already NAMES its members at the index (`unregistered_ids` /
        # `unrendered_ids`, alpha-engine-config-I7107), so it needs no
        # `_named_members` wrapper here — and cannot take one: that helper
        # routes through `aggregate()`, which raises on a `None` denominator,
        # and `of: None` is precisely how §9.1 signals uncomputable (see the
        # `_landing` comment above). The invariant both paths owe — a nonzero
        # count always carries a nonempty member list — is asserted over every
        # §9 number in `tests/test_nine_numbers.py`, not per-number here.
        "population_completeness": (
            population_completeness if population_completeness is not None
            else index.population_completeness()),                     # §9.1
        "transparency_gap": gap,                                       # §9.2
        "index_reachability": (
            reachability if reachability is not None
            else index.reachability()),                                # §9.3
        "answer_latency": index.answer_latency(),                      # §9.4
        "orphan_count": index.orphan_counts(),                         # §9.5
        "staleness_honesty": _named_members(                            # §9.6
            index.staleness_honesty(), "violations"),
        "surface_liveness": index.surface_liveness(),                   # §9.7
        "onboarding_cost": index.onboarding_cost(),                    # §9.8
        "claim_conflicts": _claim_conflicts(conflicts, entities),      # §9.9
        # Not one of the nine: the coverage number that keeps `unobserved`
        # honest (alpha-engine-config-I8765). A declared registry with no
        # observation half wired renders every row `unobserved`; without this
        # number, that reads the same as a fleet nothing is wrong with.
        "artifact_observation_coverage": _named_members(
            _artifact_observation_coverage(index), "unobserved_ids"),
        # Not one of the nine, but the same denominator-inline discipline —
        # kept here rather than dropped, since a prior response reads it.
        "not_healthy": _not_healthy(exceptions, entities),
    }


def _count_of(d: dict[str, Any]) -> tuple[int, int]:
    return d["count"], d["of"]


def _named_members(d: dict[str, Any], key: str) -> dict[str, Any]:
    """An `aggregate` that also NAMES the rows it counted (§5.1, §3.1).

    §9.6 is the one §9 number whose members are reachable NOWHERE else on the
    surface. A staleness violation is by construction a row whose rendered
    state is NOT in `render/html.py::EXCEPTION_STATES` — that is the entire
    definition of the finding — so it never appears on the exception-first
    landing view, in any facet, or in `doctor`. Publishing only `count / of`
    therefore reports a defect that no reader of the console can locate:
    establishing WHICH two rows the fleet's live `2 / 15` referred to required
    rebuilding the index by hand on the box. §5.1's evidence-link field and
    §3.1's three reachability paths both forbid that.

    Still routed through `aggregate` so §5.3's "a count without its population
    is invalid" guard applies here exactly as to every other number — EXCEPT
    where the number has already refused to render. `computable: False` is
    §5.3 being obeyed at the source rather than violated (the number declined
    to state a count over an unestablished population), so passing its `None`
    denominator to `aggregate` would turn an honest refusal into a 500. The
    refusal travels to the wire whole, reason included, exactly as
    `onboarding_cost` and `answer_latency` already do
    (alpha-engine-config-I7126).
    """
    if d.get("computable") is False:
        return dict(d)
    out = dict(aggregate(*_count_of(d)))
    out[key] = sorted(d.get(key) or ())
    for extra in ("computable", "unauditable"):
        if extra in d:
            out[extra] = d[extra]
    return out


def _list(index: Index, req: Resolved) -> dict[str, Any]:
    listing = list_rows(index, req)
    start = (req.page - 1) * 50
    shown = listing.rows[start:start + 50]
    doc = {
        "schema_version": SCHEMA_VERSION,
        "view": "list",
        "kind": req.kind.value,
        "facets": dict(req.facets),
        "page": req.page,
        # §3.4: a list showing a subset says so. A consumer that reads only
        # `entities` and never `showing`/`of` would make the same mistake a
        # reader makes with a top-10 that does not say it is a top-10.
        "showing": len(shown), "filtered": len(listing.rows),
        "of": listing.total,
        "summary": listing.summary,
        "entities": [entity(e, with_document=False) for e in shown],
    }
    if listing.dated:
        # A dated view says which window it is and what it left out (§3.4).
        doc["window"] = {"days": listing.window_days,
                         "declared_days": listing.declared_window,
                         "outside": listing.outside}
    if listing.collapsed:
        # The run list's default projection, declared rather than implied
        # (§3.4): newest run per job, with how many runs it stands for.
        doc["latest_per_job"] = {"jobs": len(listing.rows),
                                 "runs": listing.filtered_runs}
    labels = {e.id: listing.labels[e.id] for e in shown if e.id in listing.labels}
    if labels:
        doc["labels"] = labels
    from ..index import planner
    from ..index.landing import PANE_FACET

    # A dashboard's dated plan (`panes:` on a planner project): the same
    # projects, evaluated the same way, as the HTML list shows above its rows.
    planned = planner.for_pane(index, req.facets.get(PANE_FACET))
    if planned:
        doc["planner"] = {"question": planner.PLANNER_QUESTION,
                          "states": list(planner.SCHEDULE_STATES),
                          "projects": planned}
    return doc


@dataclass(frozen=True)
class Listing:
    """One list view's rows, computed once for both representations (§3.8)."""

    rows: list[Entity]
    total: int
    summary: dict[str, int]
    collapsed: bool = False
    filtered_runs: int = 0
    labels: dict[str, str] = dc_field(default_factory=dict)
    #: A dated view (`?days=`, or a window the rows declare): rows ordered
    #: newest first, never collapsed per job. `window_days` None is the
    #: archive — every row. `outside` counts filtered rows the window left out
    #: (older, or carrying no parseable as-of).
    dated: bool = False
    window_days: int | None = None
    declared_window: int | None = None
    outside: int = 0


def declared_window(entities: list[Entity]) -> int | None:
    """The window every row declares (`detail["window_days"]`), or None.

    Only when EVERY row declares the same one: a pane mixing a windowed source
    with an unwindowed one must not silently hide the second's older rows.
    """
    values = {e.detail.get(WINDOW_DETAIL) for e in entities}
    if len(values) != 1:
        return None
    (value,) = values
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def declared_labels(entities: list[Entity]) -> dict[str, str]:
    """The labels rows declare for themselves (`detail["label"]`), by id.

    Display only: the id stays the link and the identifier. A row without a
    label is absent here and shows its id.
    """
    return {e.id: e.detail[LABEL_DETAIL] for e in entities
            if isinstance(e.detail.get(LABEL_DETAIL), str) and e.detail[LABEL_DETAIL]}


def job_label(ent: Entity) -> str | None:
    """The name a run's source declares for its job (`detail["job_label"]`).

    None when it declares none, and the job is then named by itself.
    """
    raw = ent.detail.get(JOB_LABEL_DETAIL)
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


@dataclass(frozen=True)
class JobView:
    """Rows with each job's runs folded to the newest one (I11805).

    `rows` keeps every non-run row and, per job, the newest run, in the order
    each was first seen. `labels` names a row for a reader: a folded run by its
    job, any other row by the label it declares. `runs` says how many runs a
    folded row stands for, so the projection is declared, never implied (§3.4).
    """

    rows: list[Entity]
    labels: dict[str, str]
    runs: dict[str, int]


def latest_per_job(index: Index, entities: list[Entity],
                   per_state: bool = False) -> JobView:
    """Fold runs to the newest run per job; leave every other row alone.

    A job is the run's `belongs-to` target, or else the id before `@`. With
    `per_state`, a job's runs fold per state, so the landing view keeps one row
    for "job X FAILED" and another for "job X DEGRADED": each is a different
    fact, and the state groups render both.
    """
    job_of = {e.source: e.target for e in index.edges() if e.rel == "belongs-to"}
    declared = declared_labels(entities)
    order: list[object] = []
    newest: dict[object, Entity] = {}
    counts: dict[object, int] = {}
    jobs: dict[object, str] = {}
    for ent in entities:
        if ent.kind is not Kind.RUN:
            key: object = ("row", ent.id)
        else:
            job = job_of.get(ent.id) or ent.id.split("@", 1)[0]
            key = ("job", job, ent.state_value if per_state else None)
            jobs[key] = job
        kept = newest.get(key)
        if kept is None:
            order.append(key)
            newest[key] = ent
        elif (ent.provenance.as_of or "") > (kept.provenance.as_of or ""):
            newest[key] = ent
        counts[key] = counts.get(key, 0) + 1
    rows = [newest[k] for k in order]
    labels: dict[str, str] = {}
    runs: dict[str, int] = {}
    for key in order:
        ent = newest[key]
        job = jobs.get(key)
        if job is None:
            if ent.id in declared:
                labels[ent.id] = declared[ent.id]
            continue
        # A folded row stands for its JOB, so the job's name is its label:
        # the name the source declares for the job (`job_label`), else the job
        # itself. A row that is its own job (no `@`, no belongs-to edge) keeps
        # the label it declares, which names it better than its id does.
        name = job_label(ent) or job
        labels[ent.id] = declared.get(ent.id, name) if job == ent.id else name
        runs[ent.id] = counts[key]
    return JobView(rows=rows, labels=labels, runs=runs)


def _as_of_time(ent: Entity) -> datetime | None:
    raw = ent.provenance.as_of
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _dated(rows: list[Entity], window: int | None,
           now: datetime) -> tuple[list[Entity], int]:
    """Rows in the window (all when `window` is None), newest first; undated
    rows sort last in the archive and fall outside any window."""
    stamped = [(e, _as_of_time(e)) for e in rows]
    if window is not None:
        cutoff = now - timedelta(days=window)
        kept = [(e, t) for e, t in stamped if t is not None and t >= cutoff]
    else:
        kept = stamped
    floor = datetime.min.replace(tzinfo=timezone.utc)
    kept.sort(key=lambda pair: pair[1] or floor, reverse=True)
    return [e for e, _ in kept], len(rows) - len(kept)


def list_rows(index: Index, req: Resolved, now: datetime | None = None) -> Listing:
    """Filter, collapse and order a list (alpha-engine-config-I11805).

    A run list shows the newest run per job unless the URL says `runs=all`:
    434 timestamped rows answered "what ran, ever", not "what state is each
    job in", which is the question a list is opened with. Rows that are not
    healthy sort first, keeping source order otherwise, and `summary` counts
    the rows by state so a reader sees the shape before the table.
    """
    from .html import attention_order, is_exception, ordered_summary

    total = index.of_kind(req.kind)
    filtered = filter_entities(total, req.facets)
    declared = declared_window(filtered)
    if req.days_all or req.days or declared:
        window = None if req.days_all else (req.days or declared)
        rows, outside = _dated(filtered, window, now or datetime.now(timezone.utc))
        summary = {}
        for ent in rows:
            summary[ent.state_value] = summary.get(ent.state_value, 0) + 1
        return Listing(rows=rows, total=len(total), summary=ordered_summary(summary),
                       filtered_runs=len(filtered), dated=True, window_days=window,
                       declared_window=declared, outside=outside,
                       labels=declared_labels(rows))
    collapsed = req.kind is Kind.RUN and not req.all_runs
    labels: dict[str, str] = declared_labels(filtered)
    rows = filtered
    if collapsed:
        view = latest_per_job(index, filtered)
        rows, labels = view.rows, view.labels
    rows = sorted(rows, key=attention_order)
    summary: dict[str, int] = {}
    for ent in rows:
        summary[ent.state_value] = summary.get(ent.state_value, 0) + 1
    return Listing(rows=rows, total=len(total), summary=ordered_summary(summary),
                   collapsed=collapsed, filtered_runs=len(filtered), labels=labels)


def filter_entities(entities: list[Entity], facets: dict[str, str]) -> list[Entity]:
    return [e for e in entities if all(_matches(e, key, value) for key, value in facets.items())]


def _matches(entity: Entity, key: str, value: str) -> bool:
    if key in entity.facets:
        return entity.facets[key] == value
    if key == STATE_FILTER:
        # Case-insensitive: §8.3's vocabulary is upper-case, an Artifact's raw
        # value is whatever its source said, and a filter that only matched one
        # casing would render an empty list — which reads as "nothing in this
        # state" rather than "you typed it wrong" (§5.4: no data is never green).
        return entity.state_value.strip().lower() == value.strip().lower()
    for field in parse_fields(entity.detail.get("fields")):
        if field.name != key or not field.comparable or not isinstance(field.value, (int, float)):
            continue
        return {"below-baseline": field.value < field.baseline,
                "above-baseline": field.value > field.baseline,
                "at-baseline": field.value == field.baseline}.get(value, False)
    return False


def _entity_page(index: Index, ent: Entity, all_runs: bool = False) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "view": "entity",
        "entity": entity(ent),
        # Both directions (§3.3). The reverse edge is the load-bearing one:
        # "who breaks if this is stale" exists nowhere unless the index
        # derives it, and an agent asking that question needs it here.
        "relations": [edge(e) for e in index.related(ent.id)],
        # The page's projection of those edges, so HTML and JSON match (§3.8):
        # one group per relation, the related rows' states counted, runs
        # folded unless the URL says `runs=all` (alpha-engine-config-I11805).
        "relation_view": {
            "all_runs": all_runs,
            "groups": [
                {"rel": g.rel, "related": g.related, "summary": g.summary,
                 "rows": [e.id for e in g.rows], "labels": g.labels,
                 "runs": g.runs, "unresolved": g.unresolved}
                for g in relation_groups(index, ent, all_runs=all_runs)
            ],
        },
    }


@dataclass(frozen=True)
class RelationGroup:
    """One relation on an entity page, read as a list (I11805).

    `rows` are the related entities, not healthy first and newest first
    within that. Unless the URL says `runs=all`, a job's runs fold to its
    newest run per state, so a component's page shows "FAILED, newest of 3"
    rather than every run it ever had; `related` counts the rows before the
    fold and `runs` how many runs each folded row stands for, so the fold is
    declared (§3.4). `unresolved` names edge ends no entity carries: a
    relation to nothing is still a fact, never dropped.
    """

    rel: str
    rows: list[Entity]
    labels: dict[str, str]
    runs: dict[str, int]
    summary: dict[str, int]
    related: int
    unresolved: list[str]


def relation_groups(index: Index, ent: Entity, all_runs: bool = False) -> list[RelationGroup]:
    """The entity page's relations, grouped by predicate, in first-seen order.

    One implementation for both representations (§3.8). Each related id is
    listed once per predicate, however many adapters declared the edge.
    """
    from .html import attention_order, is_exception, ordered_summary

    others: dict[str, list[str]] = {}
    for e in index.related(ent.id):
        other = e.target if e.source == ent.id else e.source
        ids = others.setdefault(e.rel, [])
        if other not in ids:
            ids.append(other)
    groups: list[RelationGroup] = []
    for rel, ids in others.items():
        resolved = [index.entity(i) for i in ids]
        found = [o for o in resolved if o is not None]
        unresolved = [i for i, o in zip(ids, resolved) if o is None]
        if all_runs:
            rows, labels, runs = found, declared_labels(found), {}
        else:
            view = latest_per_job(index, found, per_state=True)
            rows, labels, runs = view.rows, view.labels, view.runs
        rows = sorted(rows, key=lambda o: o.provenance.as_of or "", reverse=True)
        rows = sorted(rows, key=attention_order)
        summary: dict[str, int] = {}
        for o in rows:
            summary[o.state_value] = summary.get(o.state_value, 0) + 1
        groups.append(RelationGroup(rel=rel, rows=rows, labels=labels, runs=runs,
                                    summary=ordered_summary(summary),
                                    related=len(found), unresolved=unresolved))
    return groups


#: The question the search page answers, rendered on it and carried in its
#: JSON (§4.4).
SEARCH_QUESTION = ("What in the fleet does this identifier or name point to, "
                   "and what state is it in?")


@dataclass(frozen=True)
class SearchGroup:
    """The search hits of one kind, read as a list (I11805).

    `rows` keep the resolver's rank (id prefix, id substring, then label)
    within each half, not healthy first. Unless the URL says `runs=all`, a job's runs fold
    to its newest run per state: a job name used to return every run it ever
    had as a bare `job@timestamp` id. `matched` counts the hits before the
    fold and `runs` how many runs a folded row stands for, so the fold is
    declared (§3.4).
    """

    kind: Kind
    rows: list[Entity]
    labels: dict[str, str]
    runs: dict[str, int]
    summary: dict[str, int]
    matched: int


@dataclass(frozen=True)
class SearchView:
    """One search, computed once for both representations (§3.8).

    `exact` are the hits whose id IS the query (§3.7): never folded, so
    pasting a full run id lands on that run even when a newer one exists.
    `groups` hold every other hit, one per kind in the model's order.
    `by_label` counts the hits found by their declared label alone — the id
    did not match — so the page can say a plain-language query found them.
    """

    query: str
    all_runs: bool
    exact: list[Entity]
    exact_labels: dict[str, str]
    groups: list[SearchGroup]
    hits: int
    by_label: int = 0


def search_view(index: Index, query: str, all_runs: bool = False) -> SearchView:
    """Resolve a query and shape its hits the way lists read (I11805)."""
    hits = search(index, query)
    exact = [h.entity for h in hits if h.exact]
    groups = kind_groups(index, [h.entity for h in hits if not h.exact], all_runs)
    return SearchView(query=query, all_runs=all_runs, exact=exact,
                      exact_labels=latest_per_job(index, exact).labels,
                      groups=groups, hits=len(hits),
                      by_label=sum(1 for h in hits if h.matched_on == MATCHED_ON_LABEL))


def kind_groups(index: Index, entities: list[Entity],
                all_runs: bool = False) -> list[SearchGroup]:
    """Rows read as lists read: one group per kind, in the model's order.

    Within a group the incoming order is kept, not healthy first; unless
    `all_runs`, a job's runs fold to its newest run per state, and the group
    counts its rows by state. Shared by search and the registry page, so the
    two cannot fold or count differently (I11805).
    """
    from .html import attention_order, is_exception, ordered_summary

    by_kind: dict[Kind, list[Entity]] = {}
    for ent in entities:
        by_kind.setdefault(ent.kind, []).append(ent)
    groups: list[SearchGroup] = []
    for kind in Kind:
        found = by_kind.get(kind)
        if not found:
            continue
        if all_runs:
            rows, labels, runs = found, declared_labels(found), {}
        else:
            view = latest_per_job(index, found, per_state=True)
            rows, labels, runs = view.rows, view.labels, view.runs
        rows = sorted(rows, key=attention_order)
        summary: dict[str, int] = {}
        for e in rows:
            summary[e.state_value] = summary.get(e.state_value, 0) + 1
        groups.append(SearchGroup(kind=kind, rows=rows, labels=labels, runs=runs,
                                  summary=ordered_summary(summary),
                                  matched=len(found)))
    return groups


def _search(index: Index, query: str, all_runs: bool = False) -> dict[str, Any]:
    hits = search(index, query)
    view = search_view(index, query, all_runs=all_runs)
    return {
        "schema_version": SCHEMA_VERSION,
        "view": "search",
        "question": SEARCH_QUESTION,
        "query": query,
        # `matched_on` says whether the id or only the declared label matched
        # (alpha-engine-config-I11805); identifier matches always rank first.
        "hits": [{"exact": h.exact, "matched_on": h.matched_on, "entity": entity(h.entity)}
                 for h in hits],
        # The page's projection of those hits, so HTML and JSON match (§3.8):
        # exact matches first, then one group per kind with its rows' states
        # counted and runs folded unless the URL says `runs=all`
        # (alpha-engine-config-I11805). `hits` above is unchanged.
        "search_view": {
            "all_runs": all_runs,
            "exact": [e.id for e in view.exact],
            "exact_labels": view.exact_labels,
            "by_label": view.by_label,
            "groups": [
                {"kind": g.kind.value, "matched": g.matched, "summary": g.summary,
                 "rows": [e.id for e in g.rows], "labels": g.labels, "runs": g.runs}
                for g in view.groups
            ],
        },
    }


REGISTRY_QUESTION = "What does this registry declare, and what state is each row in?"


@dataclass(frozen=True)
class RegistryView:
    """One registry's page, computed once for both representations (§3.8).

    `source` is the registry's own read this pass. `groups` hold the rows it
    declares, read the way lists read (I11805): one group per kind, counted
    by state, not healthy first, a job's runs folded to its newest run per
    state unless the URL says `runs=all`. `rows` counts the rows before the
    fold. The page used to show only the read's status and time, so a link
    from home or `/dashboards` led to a page that named none of its rows.
    """

    name: str
    source: Any
    all_runs: bool
    groups: list[SearchGroup]
    rows: int
    summary: dict[str, int]


def registry_view(index: Index, name: str, all_runs: bool = False) -> RegistryView:
    source = next(a for a in index.build_info.adapters if a.name == name)
    from .html import ordered_summary

    claimed = index.claimed_by(name)
    groups = kind_groups(index, claimed, all_runs)
    summary: dict[str, int] = {}
    for g in groups:
        for state, n in g.summary.items():
            summary[state] = summary.get(state, 0) + n
    return RegistryView(name=name, source=source, all_runs=all_runs, groups=groups,
                        rows=len(claimed), summary=ordered_summary(summary))


def _registry(index: Index, name: str, all_runs: bool = False) -> dict[str, Any]:
    view = registry_view(index, name, all_runs=all_runs)
    source = view.source
    return {
        "schema_version": SCHEMA_VERSION, "view": "registry", "name": source.name,
        "status": source.status, "fetched_at": source.fetched_at,
        "unavailable": list(source.unavailable),
        "question": REGISTRY_QUESTION,
        # The page's rows, so HTML and JSON match (§3.8): one group per kind,
        # counted by state, runs folded unless the URL says `runs=all`
        # (alpha-engine-config-I11805). The fields above are unchanged.
        "registry_view": {
            "all_runs": all_runs,
            "cadence_seconds": source.cadence_seconds,
            "elapsed_seconds": source.elapsed_seconds,
            "rows": view.rows,
            "summary": view.summary,
            "groups": [
                {"kind": g.kind.value, "matched": g.matched, "summary": g.summary,
                 "rows": [e.id for e in g.rows], "labels": g.labels, "runs": g.runs}
                for g in view.groups
            ],
        },
    }


# ------------------------------------------------------------ projections --

def entity(ent: Entity, with_document: bool = True) -> dict[str, Any]:
    """One entity on the wire, carrying its §5.1 provenance in full.

    ``with_document=False`` (list payloads) keeps a carried document's size and
    truncation but not its text: fifty reports' bodies are not a list row.

    Per-field sources are included rather than collapsed: a merged row names
    the source of each field (§2.5), and a consumer that has to ask "which
    adapter said this" is back to re-deriving.
    """
    doc: dict[str, Any] = {
        "kind": ent.kind.value,
        "id": ent.id,
        "url": ent.route,
        "state": ent.state_value,
        "provenance": _provenance(ent),
        "facets": dict(ent.facets),
        "detail": dict(ent.detail),
    }
    doc_body = ent.detail.get(DOCUMENT_DETAIL)
    if not with_document and isinstance(doc_body, dict):
        doc["detail"][DOCUMENT_DETAIL] = {k: v for k, v in doc_body.items() if k != "text"}
    if ent.field_sources:
        doc["field_sources"] = {
            name: {"source": p.source, "as_of": p.as_of, "evidence": p.evidence}
            for name, p in sorted(ent.field_sources.items())
        }
    if ent.superseded:
        # A claim that lost stays on the wire for the same reason it stays on
        # the page (§2.5): the disagreement is information, and a consumer
        # given only the verdict cannot check it.
        doc["superseded"] = [
            {
                "field": field,
                "value": value.value if isinstance(value, State) else value,
                "source": prov.source,
            }
            for field, value, prov in ent.superseded
        ]
    if ent.conflicts:
        doc["conflicts"] = list(ent.conflicts)
    return doc


def edge(e: Edge) -> dict[str, str]:
    return {"source": e.source, "rel": e.rel, "target": e.target}


def _provenance(ent: Entity) -> dict[str, Any]:
    p = ent.provenance
    return {"source": p.source, "as_of": p.as_of, "evidence": p.evidence}


def index_dump(index: Index) -> dict[str, Any]:
    """The whole index — every entity and every forward edge.

    Used by `console index` and by nothing else on the request path: a route
    is a *query*, and "give me everything" is the one query no HTTP consumer
    should be encouraged to make on a surface whose job is to say what is
    wrong.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "entities": [entity(e) for e in index.all()],
        "edges": [edge(e) for e in index.edges()],
    }
