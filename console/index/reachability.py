"""Executed §3.1 reachability checks used by the §9.3 published number."""
from __future__ import annotations

from ..model.entity import Entity
from ..model.kinds import Kind
from ..search.resolve import search
from ..server.router import resolve
from .graph import Index


def measure(index: Index) -> dict[str, object]:
    """Execute name, structure, and relation paths for every indexed entity.

    Presence in the index is deliberately not evidence that either search or
    generated structure works.  Each path is invoked here, so a broken path is
    visible in its own published count rather than hidden by a blended ratio.

    Each entity's search- and structure-reachability is computed exactly
    ONCE and reused for both its own tally and the three-way intersection
    (alpha-engine-config-I10615: the original computed each of them twice per
    entity — once for `search_reachable`/`structure_reachable`, again inside
    `reachable_all_three` — which doubled the cost of the single most
    expensive number on the landing page for no reason). Structure
    reachability is additionally memoised PER KIND, not per entity: the
    generated-route check is "is this id in `index.of_kind(this entity's
    kind)`", and that set is identical for every entity of the same kind, so
    building it once per kind turns an O(n) scan per entity into an O(1)
    lookup after the first.

    The name path calls the search resolver for each entity's exact hits.
    These use the same snapshot lookup that ranks exact hits first in a full
    search. The check therefore executes the resolver without scanning every
    other entity's identifier and labels for each query.
    """
    entities = index.all()
    total = len(entities)
    by_kind_ids: dict[Kind, set[str]] = {}

    def structure_reaches(entity: Entity) -> bool:
        kind = entity.kind
        request = resolve(f"/{kind.route}")
        if request.kind is not kind:
            return False
        ids = by_kind_ids.get(kind)
        if ids is None:
            ids = {e.id for e in index.of_kind(kind)}
            by_kind_ids[kind] = ids
        return entity.id in ids

    search_reachable = 0
    structure_reachable = 0
    relation_reachable = 0
    reachable_all_three = 0
    for entity in entities:
        s = _search_reaches(index, entity.id)
        st = structure_reaches(entity)
        r = bool(index.inbound(entity.id))
        search_reachable += s
        structure_reachable += st
        relation_reachable += r
        reachable_all_three += s and st and r

    return {
        "total": total,
        "search_reachable": search_reachable,
        "structure_reachable": structure_reachable,
        "relation_reachable": relation_reachable,
        "reachable_all_three": reachable_all_three,
        "ratio": round(reachable_all_three / total, 4) if total else None,
    }


def _search_reaches(index: Index, entity_id: str) -> bool:
    """The search resolver must return the entity for its exact identifier."""
    return any(
        hit.exact and hit.entity.id == entity_id
        for hit in search(index, entity_id, exact_only=True)
    )
