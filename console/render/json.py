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

from typing import Any

from datetime import datetime, timezone

from ..index.graph import Index
from ..model.entity import Edge, Entity
from ..model.kinds import State
from ..model.fields import parse as parse_fields
from ..search.resolve import search
from ..server.router import Resolved

#: The wire version of this projection. Distinct from the adapter envelope's
#: version (`model/envelope.py`): one is what an adapter hands the index, the
#: other is what the index hands a consumer, and they change for different
#: reasons.
SCHEMA_VERSION = 1


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
        "stale": info.is_stale(now),
        "staleness_basis": info.staleness_basis(),
        "age_seconds": info.age_seconds(now),
        "stale_since": info.stale_since,
        "last_error": info.last_error,
        "sources": [
            {
                "name": a.name, "status": a.status, "fetched_at": a.fetched_at,
                "cadence_seconds": a.cadence_seconds,
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
    from ..render.html import is_exception

    entities = index.all()
    exceptions = [e for e in entities if is_exception(e)]
    conflicts = index.conflicts()
    gap = index.transparency_gap()
    return {
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
        "numbers": numbers(index, exceptions, conflicts, gap),
    }


#: §9.7 (surface liveness) is explicitly out of THIS issue's scope — it
#: belongs to the console's deploy work (nous-ergon-ops-I364) rather than to
#: the index/render layer this issue covers. §11's carve-out requires the
#: number be named and the cycle it is expected by stated, not silently
#: omitted — this is that statement.
_SURFACE_LIVENESS_NOT_IMPL = {
    "state": "N/A-NOT-IMPL",
    "expected_cycle": "console deploy work (nous-ergon-ops-I364)",
    "reason": (
        "surface liveness needs a watcher component that is NOT the console "
        "itself (§9.7) — that watcher is deploy/ops work, not an index "
        "computation, and is tracked separately"
    ),
}


def numbers(index: Index, exceptions: list[Entity], conflicts: list[Entity],
           gap: dict[str, Any]) -> dict[str, Any]:
    """console-policy.md §9 — all nine numbers, one dict, one place they are
    assembled for both representations (§3.8: the HTML numbers section reads
    the same shape via `render.html.landing_numbers`)."""
    entities = index.all()
    return {
        "population_completeness": index.population_completeness(),   # §9.1
        "transparency_gap": aggregate(gap["count"], gap["of"]),        # §9.2
        "index_reachability": index.reachability(),                    # §9.3
        "answer_latency": index.answer_latency(),                      # §9.4
        "orphan_count": index.orphan_counts(),                         # §9.5
        "staleness_honesty": aggregate(*_count_of(index.staleness_honesty())),  # §9.6
        "surface_liveness": _SURFACE_LIVENESS_NOT_IMPL,                 # §9.7
        "onboarding_cost": index.onboarding_cost(),                    # §9.8
        "claim_conflicts": aggregate(len(conflicts), len(entities)),   # §9.9
        # Not one of the nine, but the same denominator-inline discipline —
        # kept here rather than dropped, since a prior response reads it.
        "not_healthy": aggregate(len(exceptions), len(entities)),
    }


def _count_of(d: dict[str, Any]) -> tuple[int, int]:
    return d["count"], d["of"]


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
