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
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from ..index.graph import Index
from ..index.milestones import evaluate as evaluate_milestones
from ..index.milestones import journal_report as milestone_journal_report
from ..index.numbers import artifact_observation_coverage as _artifact_observation_coverage
from ..index.numbers import claim_conflicts as _claim_conflicts
from ..index.numbers import not_healthy as _not_healthy
from ..model.entity import Edge, Entity
from ..model.kinds import STATE_FILTER, State
from ..model.fields import parse as parse_fields
from ..search.resolve import search
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
    elif req.view == "entity":
        ent = index.entity(req.entity_id or "")
        if ent is None:  # pragma: no cover - app.py 404s before reaching here
            raise KeyError(req.entity_id)
        doc = _entity_page(index, ent)
    elif req.view == "history":
        ent = index.entity(req.entity_id or "")
        if ent is None:
            raise KeyError(req.entity_id)
        from ..history import query as history_query
        doc = {"schema_version": SCHEMA_VERSION, "view": "history",
               "entity": entity(ent), "history": history_query(ent, req.window_hours or 24)}
    elif req.view == "search":
        doc = _search(index, req.query or "")
    elif req.view == "doctor":
        from ..diagnose import as_dict, doctor

        doc = {"schema_version": SCHEMA_VERSION, "view": "doctor",
               **as_dict(doctor(index, req.query or ""))}
    elif req.view == "registry":
        source = next(a for a in index.build_info.adapters if a.name == req.registry_name)
        doc = {"schema_version": SCHEMA_VERSION, "view": "registry", "name": source.name,
               "status": source.status, "fetched_at": source.fetched_at,
               "unavailable": list(source.unavailable)}
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
    order, as a human looking at the page.
    """
    from ..render.html import landing_exceptions

    entities = index.all()
    exceptions = landing_exceptions(index)
    conflicts = index.conflicts()
    gap = index.transparency_gap()
    n = numbers(index, exceptions, conflicts, gap)
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "registry_pages": index.registry_coverage(),
        "view": "landing",
        "exceptions": [entity(e) for e in exceptions],
        # §4.3's third element: what is waiting on Brian. Same query the HTML
        # renders (§3.8) — `Index.decision_queue()`, filtered to
        # `decision-queue-policy.md` §2's own labels, not every open Decision.
        "decision_queue": [entity(e) for e in index.decision_queue()],
        # console-policy.md §9 — the nine numbers, all published, all stating
        # their denominator inline (§5.3). A number this build could not
        # compute renders `state: N/A-NOT-IMPL` with the cycle it is expected
        # by (§11) — except §9.1/§9.2, which are never allowed that token:
        # §9.1 (population_completeness) signals uncomputable with
        # `of`/`ratio` both `None` (not wrapped in `aggregate()`, which would
        # reject a missing denominator outright), and §9.2 always has a real
        # denominator (the component population), so it never needs to.
        "numbers": n,
    }
    # console-policy.md §4.4's milestone pane, from the SAME assembly the HTML
    # renders (`render.html.milestones_section` calls the same `evaluate` over
    # the same `numbers` dict), so the two representations of this URL cannot
    # answer the predicate differently.
    #
    # The key is ABSENT when nothing is declared, rather than an empty list: a
    # deployment that declares no milestone has no milestone pane, and an empty
    # `milestones: []` reads to a consumer like a declared predicate with no
    # clauses — which `parse` refuses to build in the first place.
    declared_milestones = evaluate_milestones(index, n)
    if declared_milestones:
        doc["milestones"] = declared_milestones
    # What the clause journal did on THIS build: the transitions it recorded,
    # the episodes still open, and — loudly — any failure to record at all. A
    # recorder that stopped working is a fact about the surface's own honesty
    # and must be readable by the agents that read this view, not only by a
    # human looking at the page (§3.8).
    recorded = milestone_journal_report(index)
    if recorded:
        doc["milestone_journal"] = [dict(r) for r in recorded]
    return doc


#: §9.7 (surface liveness) is explicitly out of THIS issue's scope — it
#: belongs to the console's deploy work (nous-ergon-ops-I364) rather than to
#: the index/render layer this issue covers. §11's carve-out requires the
#: number be named and the cycle it is expected by stated, not silently
#: omitted — this is that statement.
def numbers(index: Index, exceptions: list[Entity], conflicts: list[Entity],
           gap: dict[str, Any]) -> dict[str, Any]:
    """console-policy.md §9 — all nine numbers, one dict, one place they are
    assembled for both representations (§3.8: the HTML numbers section reads
    the same shape via `render.html.landing_numbers`)."""
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
        "population_completeness": index.population_completeness(),   # §9.1
        "transparency_gap": gap,                                       # §9.2
        "index_reachability": index.reachability(),                    # §9.3
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
    total = index.of_kind(req.kind)
    filtered = filter_entities(total, req.facets)
    start = (req.page - 1) * 50
    shown = filtered[start:start + 50]
    return {
        "schema_version": SCHEMA_VERSION,
        "view": "list",
        "kind": req.kind.value,
        "facets": dict(req.facets),
        "page": req.page,
        # §3.4: a list showing a subset says so. A consumer that reads only
        # `entities` and never `showing`/`of` would make the same mistake a
        # reader makes with a top-10 that does not say it is a top-10.
        "showing": len(shown), "filtered": len(filtered),
        "of": len(total),
        "entities": [entity(e) for e in shown],
    }


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


def _entity_page(index: Index, ent: Entity) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "view": "entity",
        "entity": entity(ent),
        # Both directions (§3.3). The reverse edge is the load-bearing one:
        # "who breaks if this is stale" exists nowhere unless the index
        # derives it, and an agent asking that question needs it here.
        "relations": [edge(e) for e in index.related(ent.id)],
    }


def _search(index: Index, query: str) -> dict[str, Any]:
    hits = search(index, query)
    return {
        "schema_version": SCHEMA_VERSION,
        "view": "search",
        "query": query,
        "hits": [{"exact": h.exact, "entity": entity(h.entity)} for h in hits],
    }


# ------------------------------------------------------------ projections --

def entity(ent: Entity) -> dict[str, Any]:
    """One entity on the wire, carrying its §5.1 provenance in full.

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
