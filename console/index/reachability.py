"""Executed §3.1 reachability checks used by the §9.3 published number."""
from __future__ import annotations

from ..search.resolve import search
from ..server.router import resolve


def measure(index) -> dict[str, object]:
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

    `_search_reaches` still calls the real `search()` resolver once per
    entity — deliberately not short-circuited to a cheaper index-only lookup
    — because the point of this path is to prove the ACTUAL search resolver
    still answers for this id, not to assume it does from the data search()
    would use anyway (`test_reachability_measures_each_path_independently`
    asserts this by monkeypatching `search` itself and expecting the count to
    go to 0). `search()`'s own O(n) per-query scan is therefore the
    irreducible cost of this path; only the STRUCTURE path's repeated re-scan
    was avoidable, and it now composes to one dict-build per kind.
    """
    entities = index.all()
    total = len(entities)
    by_kind_ids: dict[object, set[str]] = {}

    def structure_reaches(entity) -> bool:
        request = resolve(f"/{entity.kind.route}")
        if request.kind is not entity.kind:
            return False
        ids = by_kind_ids.get(request.kind)
        if ids is None:
            ids = {e.id for e in index.of_kind(request.kind)}
            by_kind_ids[request.kind] = ids
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


def _search_reaches(index, entity_id: str) -> bool:
    """The search resolver must return the entity for its exact identifier."""
    return any(hit.exact and hit.entity.id == entity_id for hit in search(index, entity_id))
