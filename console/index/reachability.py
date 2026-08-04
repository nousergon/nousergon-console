"""Executed §3.1 reachability checks used by the §9.3 published number."""
from __future__ import annotations

from ..search.resolve import search
from ..server.router import resolve


def measure(index) -> dict[str, object]:
    """Execute name, structure, and relation paths for every indexed entity.

    Presence in the index is deliberately not evidence that either search or
    generated structure works.  Each path is invoked here, so a broken path is
    visible in its own published count rather than hidden by a blended ratio.
    """
    entities = index.all()
    total = len(entities)
    search_reachable = sum(_search_reaches(index, entity.id) for entity in entities)
    structure_reachable = sum(_structure_reaches(index, entity) for entity in entities)
    relation_reachable = sum(bool(index.inbound(entity.id)) for entity in entities)
    reachable_all_three = sum(
        _search_reaches(index, entity.id)
        and _structure_reaches(index, entity)
        and bool(index.inbound(entity.id))
        for entity in entities
    )
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


def _structure_reaches(index, entity) -> bool:
    """The generated kind-list navigation route must terminate at the entity."""
    request = resolve(f"/{entity.kind.route}")
    return request.kind is entity.kind and any(
        listed.id == entity.id for listed in index.of_kind(request.kind)
    )
