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
from ..model.fields import Field, format_value, parse as parse_fields
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


def fields_section(ent: Entity) -> str:
    """A module's own data, rendered from its descriptors alone (§5.8).

    Nothing below branches on WHO emitted the field. That is the whole claim:
    the console renders data from a module it has never heard of, and the moment
    this function grows a check for a component id, a repo or a domain it has
    become the per-module rendering path §5.8 forbids.
    """
    declared = parse_fields(ent.detail.get("fields"))
    if not declared:
        return ""
    undeclared = [f for f in declared if not f.declared]
    rows = "".join(_field_row(f) for f in declared)
    # §5.8: an undeclared field renders opaque and is COUNTED. A dropped field
    # is a fact the emitter believes is on the surface and is not, and it fails
    # on their side of a boundary they cannot see.
    note = (
        f'<p class="absent">{len(undeclared)} of {len(declared)} fields are not '
        "fully declared — rendered opaque rather than dropped (§5.8)</p>"
        if undeclared else ""
    )
    return (
        "<h2>declared fields</h2>"
        f"{note}"
        "<table><thead><tr><th>field</th><th>value</th><th>unit</th>"
        "<th>baseline</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _field_row(f: Field) -> str:
    """One field. Colour appears only where a baseline was declared (§5.4)."""
    unit = esc(f.unit) if f.unit else '<em class="absent">no unit</em>'
    if f.comparable:
        baseline = esc(f.baseline)
        css = "field-comparable"
    elif f.baseline_declared:
        # An explicitly declared absence of baseline. §5.4: the number is
        # telemetry, not a verdict — plain, uncoloured, unlabelled by quality.
        baseline = '<em class="absent">none declared — rendered as telemetry</em>'
        css = "field-telemetry"
    else:
        baseline = '<em class="absent">not stated</em>'
        css = "field-telemetry"
    defect = (
        f'<br><small class="absent">{esc(f.defect)}</small>' if f.defect else ""
    )
    return (
        f'<tr class="{css} render-{esc(f.render.value)}">'
        f"<td>{esc(f.name)}{defect}</td>"
        f"<td>{esc(format_value(f))}</td>"
        f"<td>{unit}</td><td>{baseline}</td></tr>"
    )


def _claims_section(ent: Entity) -> str:
    """What several sources said about this entity, and who won (§2.5).

    Both halves are rendered on purpose. The winners answer §5.1's "how does
    this row know what it claims", per field rather than once. The losers are
    the half a merge normally destroys — "systemd says this unit is masked" is
    worth reading next to "the registry says it is in service", and a surface
    that shows only the verdict cannot be checked by the person reading it.
    """
    if not ent.field_sources and not ent.superseded and not ent.conflicts:
        return ""
    won = "".join(
        f"<tr><td>{esc(f)}</td><td>{esc(p.source)}</td>"
        f'<td>{esc(p.as_of) if p.as_of else "<em>no stamp</em>"}</td></tr>'
        for f, p in sorted(ent.field_sources.items())
    )
    lost = "".join(
        f"<li>{esc(f)} = <code>{esc(_value_of(v))}</code> "
        f"<small>from {esc(p.source)}</small></li>"
        for f, v, p in ent.superseded
    ) or '<li class="absent">nothing superseded — one source supplied this row</li>'
    conflict = (
        f'<p class="state-DEGRADED">unresolved disagreement on: '
        f'{esc(", ".join(ent.conflicts))} — two sources of equal standing '
        f"disagree, and neither is authoritative over the other (§2.5)</p>"
        if ent.conflicts else ""
    )
    return (
        "<h2>claims</h2>"
        f"{conflict}"
        "<table><thead><tr><th>field</th><th>source</th><th>as-of</th></tr></thead>"
        f"<tbody>{won}</tbody></table>"
        f"<h3>superseded</h3><ul>{lost}</ul>"
    )


def _value_of(value: object) -> str:
    return value.value if isinstance(value, State) else str(value)


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
{fields_section(ent)}
{_claims_section(ent)}
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
    conflicts = index.conflicts()
    reach = index.reachability()
    ratio = reach["ratio"]
    ratio_txt = (
        f'{reach["reachable_all_three"]} / {reach["total"]}'
        if ratio is not None else "no entities yet"
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>fleet</title></head><body>
<h1>fleet — exceptions</h1>
<p>{len(exceptions)} not healthy · {len(unreported)} unreported · {len(conflicts)} claim conflicts · index reachability {esc(ratio_txt)}</p>
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
