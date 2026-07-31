"""Global search — the §3.7 identifier resolver.

One entry point resolving any identifier the fleet uses: a component id, run
id, cycle date, object key or prefix, tracker ref, clause id, incident id,
metric name, repo, PR number. Results are typed by kind and rank exact
identifier matches first.

Search is over the index, not over a page (§3.7) — it resolves against the
same entity ids the index holds, so a search that finds nothing is a true
negative (the identifier is not in the fleet), never a silently-excluded kind.
Every kind is in scope; an absent kind would be unreachable on path one of
three and its absence would be invisible.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..index.graph import Index
from ..model.entity import Entity


@dataclass(frozen=True)
class Hit:
    entity: Entity
    exact: bool  # exact identifier match ranks first (§3.7)


def search(index: Index, query: str) -> list[Hit]:
    """Resolve a query string to ranked hits over every kind.

    Ranking: exact id matches first, then prefix matches, then substring
    matches — so pasting a full identifier lands directly on the entity, and a
    partial identifier still surfaces candidates. All kinds are searched.
    """
    q = query.strip()
    if not q:
        return []
    ql = q.lower()
    exact: list[Hit] = []
    prefix: list[Hit] = []
    substring: list[Hit] = []
    for ent in index.all():
        eid = ent.id.lower()
        if eid == ql:
            exact.append(Hit(ent, exact=True))
        elif eid.startswith(ql):
            prefix.append(Hit(ent, exact=False))
        elif ql in eid:
            substring.append(Hit(ent, exact=False))
    return exact + prefix + substring
