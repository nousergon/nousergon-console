"""Server-side HTML rendering — the four-field row contract (§5.1).

Every rendered fact carries state · source · as-of · evidence link. A dot that
cannot say how it knows is not yet trustworthy, so the row renderer takes an
Entity (whose provenance is required at construction) and always emits all
four fields. Server-side rendering is what makes §3.2's identity-is-the-URL
structural: the HTML for a URL is a pure function of the resolved request and
the index, with no client state to reconstruct.

Rendering rules honoured here:
- Absence renders as itself (§5.5): UNREPORTED, NEVER_RAN, MISSED and
  ABSENT are four different facts and render as four different things, never
  drawn as green and never as nothing.
- A number without a baseline is telemetry, not a verdict (§5.4): states are
  labelled, not colour-coded by quality.
- The exception list is the default (§4.3): the landing view leads with what
  is not HEALTHY, with owner and age, then the transparency-gap count.
"""
from __future__ import annotations

import html

from ..index.graph import Index
from ..model.entity import Entity
from ..model.kinds import EXCEPTION_VALUES, Kind, State
from ..server.router import path_for_entity

#: Component states that mean "look at me" on the exception-first landing view
#: (§4.3). The three DECLARED states are deliberately absent: DISABLED,
#: DEPRECATED and RETIRED are decisions already taken, and paging someone about
#: a decision is what observability-policy.md §8.3's DISABLED/MISSED pair
#: exists to prevent. NEVER_RAN IS here — a component that has never executed
#: has never been tested, and its first failure is still ahead of it.
EXCEPTION_STATES = frozenset({
    State.FAILED, State.STALLED, State.MISSED, State.DEGRADED,
    State.UNREPORTED, State.ABSENT, State.UNREGISTERED, State.NEVER_RAN,
})


def is_exception(ent: Entity) -> bool:
    """Whether this row belongs on the exception-first landing view (§4.3).

    Handles both halves of §5.1: a component state is checked against the
    twelve, and a raw value (an Artifact's freshness, a tracker's open/closed)
    against the small set of values that mean the same thing. An open decision
    is NOT an exception — it is the "waiting on Brian" half of the same view.
    """
    if isinstance(ent.state, State):
        return ent.state in EXCEPTION_STATES
    return str(ent.state).strip().lower() in EXCEPTION_VALUES


def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def row(ent: Entity) -> str:
    """One four-field row: state · source · as-of · evidence (§5.1)."""
    p = ent.provenance
    as_of = esc(p.as_of) if p.as_of else '<em class="absent">no freshness stamp</em>'
    evidence = (
        f'<a href="{esc(p.evidence)}">evidence</a>' if p.evidence
        else '<em class="absent">no link</em>'
    )
    return (
        f'<tr class="state-{esc(ent.state_value)}">'
        f'<td><a href="{esc(path_for_entity(ent.kind, ent.id))}">{esc(ent.id)}</a></td>'
        f"<td>{esc(ent.state_value)}</td>"
        f"<td>{esc(p.source)}</td>"
        f"<td>{as_of}</td>"
        f"<td>{evidence}</td>"
        f"</tr>"
    )


def _table(entities: list[Entity]) -> str:
    if not entities:
        # Absence renders as itself — an empty state is a rendered fact, not
        # a blank region (§5.5).
        return '<p class="absent">No entities — the source reported none.</p>'
    rows = "".join(row(e) for e in entities)
    return (
        "<table><thead><tr>"
        "<th>id</th><th>state</th><th>source</th><th>as-of</th><th>evidence</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def entity_page(index: Index, ent: Entity) -> str:
    """Everything known about one thing, including its relations (§4.1)."""
    related = index.related(ent.id)
    rel_items = "".join(
        f'<li>{esc(e.rel)} → <a href="{esc(_edge_href(index, e))}">{esc(_edge_other(ent, e))}</a></li>'
        for e in related
    ) or '<li class="absent">no relations</li>'
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{esc(ent.id)} · {esc(ent.kind.value)}</title></head><body>
<nav><a href="/">fleet</a> &rsaquo; <a href="/{esc(ent.kind.route)}">{esc(ent.kind.value)}</a> &rsaquo; {esc(ent.id)}</nav>
<h1>{esc(ent.id)}</h1>
<p class="state-{esc(ent.state_value)}">state: {esc(ent.state_value)}</p>
{_table([ent])}
<h2>relations</h2><ul>{rel_items}</ul>
</body></html>"""


def list_page(index: Index, kind: Kind, facets: dict[str, str]) -> str:
    """A filtered list — the facets are in the URL, so this reproduces cold (§3.4)."""
    entities = index.of_kind(kind)
    for fkey, fval in facets.items():
        entities = [e for e in entities if e.facets.get(fkey) == fval]
    total = len(index.of_kind(kind))
    # §3.4: a filtered list states showing N of M, never a silent subset.
    shown = f"<p>showing {len(entities)} of {total}</p>" if facets else ""
    title = f"{kind.value}s"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{esc(title)}</title></head><body>
<nav><a href="/">fleet</a> &rsaquo; {esc(title)}</nav>
<h1>{esc(title)}</h1>{shown}
{_table(entities)}
</body></html>"""


def landing_page(index: Index) -> str:
    """The exception-first default view (§4.3): what is not HEALTHY, with
    state and age, then the transparency-gap count. No aggregate green light."""
    exceptions = [e for e in index.all() if is_exception(e)]
    unreported = [e for e in index.all() if e.state is State.UNREPORTED]
    reach = index.reachability()
    ratio = reach["ratio"]
    ratio_txt = (
        f'{reach["reachable_all_three"]} / {reach["total"]}'
        if ratio is not None else "no entities yet"
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>fleet</title></head><body>
<h1>fleet — exceptions</h1>
<p>{len(exceptions)} not healthy · {len(unreported)} unreported · index reachability {esc(ratio_txt)}</p>
{_table(exceptions)}
</body></html>"""


def search_page(hits: list, query: str) -> str:
    items = "".join(
        f'<li><a href="{esc(path_for_entity(h.entity.kind, h.entity.id))}">'
        f"{esc(h.entity.id)}</a> <small>{esc(h.entity.kind.value)}"
        f'{" · exact" if h.exact else ""}</small></li>'
        for h in hits
    ) or '<li class="absent">no matches — this identifier is not in the fleet</li>'
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>search · {esc(query)}</title></head><body>
<h1>search</h1><p>results for &ldquo;{esc(query)}&rdquo;</p><ul>{items}</ul>
</body></html>"""


def _edge_other(ent: Entity, edge) -> str:
    return edge.target if edge.source == ent.id else edge.source


def _edge_href(index: Index, edge) -> str:
    other = index.entity(edge.target) or index.entity(edge.source)
    if other is None:
        return "#"
    return path_for_entity(other.kind, other.id)
