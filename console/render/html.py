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
from datetime import datetime, timezone

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


def _source_findings_section(ent: Entity) -> str:
    """Render a driver's named unavailable result rather than hiding it in detail."""
    findings = [
        (name, value) for name, value in ent.detail.items()
        if name.endswith("_source") and isinstance(value, dict)
    ]
    if not findings:
        return ""
    items = "".join(
        f"<li>{esc(name)}: {esc(value.get('condition', 'unavailable'))}</li>"
        for name, value in findings
    )
    return f"<h2>source findings</h2><ul>{items}</ul>"


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
{index_freshness(index)}
<p class="state-{esc(ent.state_value)}">state: {esc(ent.state_value)}</p>
{_table([ent])}
<h2>relations</h2><ul>{rel_items}</ul>
{fields_section(ent)}
{_source_findings_section(ent)}
{_claims_section(ent)}
</body></html>"""


def history_page(index: Index, ent: Entity, window_hours: int) -> str:
    from ..history import query as history_query
    result = history_query(ent, window_hours)
    if not result["available"]:
        body = f'<p class="absent">history unavailable — {esc(result["reason"])}</p>'
    else:
        bound = (f' requested {result["requested_hours"]}h, bounded to {result["effective_hours"]}h by source retention'
                 if result["bounded"] else f' {result["effective_hours"]}h retained window')
        rows = "".join(f'<li>{esc(point)}</li>' for point in result["points"]) or '<li class="absent">no observations in this retained window</li>'
        body = f'<p>history window:{esc(bound)}</p><ul>{rows}</ul>'
    return f'<!doctype html><html><head><meta charset="utf-8"><title>history · {esc(ent.id)}</title></head><body><h1>history: {esc(ent.id)}</h1>{index_freshness(index)}{body}</body></html>'


def list_page(index: Index, kind: Kind, facets: dict[str, str], page: int = 1) -> str:
    """A filtered list — the facets are in the URL, so this reproduces cold (§3.4)."""
    entities = index.of_kind(kind)
    for fkey, fval in facets.items():
        entities = [e for e in entities if e.facets.get(fkey) == fval]
    total = len(index.of_kind(kind))
    start = (page - 1) * 50
    visible = entities[start:start + 50]
    shown = f"<p>showing {len(visible)} of {total} · {len(entities)} filtered · page {page}</p>"
    title = f"{kind.value}s"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{esc(title)}</title></head><body>
<nav><a href="/">fleet</a> &rsaquo; {esc(title)}</nav>
<h1>{esc(title)}</h1>
{index_freshness(index)}{shown}
{_table(visible)}
</body></html>"""


def index_freshness(index: Index, now: datetime | None = None) -> str:
    """The index's own as-of, rendered (§5.9).

    It is one fact about one index, so it renders at surface level rather than
    row by row — and it renders on EVERY page, because a reader who arrived on
    an entity page deep-linked from an alert is exactly the reader who must not
    assume what they are seeing is current.
    """
    info = index.build_info
    now = now or datetime.now(timezone.utc)
    if not info.built_at:
        return ('<p class="absent">index build time unknown — this surface '
                "cannot say how current it is (§5.9)</p>")
    sources = ", ".join(
        f"{a.name} {a.status}" for a in info.adapters
    ) or "no sources"
    cadence = (
        f"rebuilds every {info.refresh_seconds:g}s"
        if info.refresh_seconds else "no rebuild cadence declared"
    )
    if info.is_stale(now):
        # The whole surface, not row by row. Every row below is at most as
        # current as this, and rows that look internally consistent with each
        # other are exactly how a frozen surface passes for a live one.
        return (
            f'<p class="state-MISSED">SURFACE STALE — index built '
            f"{esc(info.built_at)}, {esc(info.staleness_basis())}"
            + (f", {esc(info.last_error)}" if info.last_error else "")
            + f". {esc(cadence)}. sources: {esc(sources)}</p>"
        )
    return (
        f'<p class="index-fresh">index built {esc(info.built_at)} · '
        f"{esc(cadence)} · {esc(info.staleness_basis())} · "
        f"sources: {esc(sources)}</p>"
    )


def landing_page(index: Index) -> str:
    """The exception-first default view (§4.3): what is not HEALTHY, with
    state and age · the transparency-gap count · what is waiting on Brian
    (the decision queue) · the completeness ratio. No aggregate green light."""
    exceptions = [e for e in index.all() if is_exception(e)]
    unreported = [e for e in index.all() if e.state is State.UNREPORTED]
    conflicts = index.conflicts()
    reach = index.reachability()
    ratio = reach["ratio"]
    ratio_txt = (
        f'{reach["reachable_all_three"]} / {reach["total"]}'
        if ratio is not None else "no entities yet"
    )
    registries = index.registry_coverage()
    registry_txt = f'{registries["count"]} / {registries["of"]}'
    missing = (f' · missing: {esc(", ".join(registries["missing"]))}'
               if registries["missing"] else "")
    queue = index.decision_queue()
    completeness = index.population_completeness()
    completeness_txt = (
        f'{completeness["rendered"]} / {completeness["of"]} '
        f'({completeness["ratio"]:.0%})'
        if completeness["ratio"] is not None
        else "unknown — no registry configured (§9.1)"
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>fleet</title></head><body>
<h1>fleet — exceptions</h1>
<form action="/search" method="get"><label for="global-search">search fleet</label> <input id="global-search" name="q" accesskey="/" autocomplete="off"><button type="submit">search</button></form>
{index_freshness(index)}
<h2>registries</h2><ul>{''.join(f'<li><a href="/registry/{esc(name)}">{esc(name)}</a></li>' for name in index.registry_names()) or '<li class="absent">none declared</li>'}</ul>
<p>registry pages {esc(registry_txt)}{missing} · {len(exceptions)} not healthy · {len(unreported)} unreported (transparency gap, §9.2) · {len(conflicts)} claim conflicts · index reachability {esc(ratio_txt)}</p>
{_table(exceptions)}
<h2>waiting on Brian</h2>
{_table(queue)}
<p>population completeness {esc(completeness_txt)} · {completeness["unregistered"]} unregistered (§9.1)</p>
</body></html>"""


def registry_page(index: Index, name: str) -> str:
    source = next(a for a in index.build_info.adapters if a.name == name)
    return f"""<!doctype html><html><head><meta charset=\"utf-8\"><title>{esc(name)} registry</title></head><body>
<nav><a href=\"/\">fleet</a> &rsaquo; registry</nav><h1>registry: {esc(name)}</h1>
{index_freshness(index)}<p>source status: {esc(source.status)} · fetched: {esc(source.fetched_at or 'no freshness stamp')}</p>
</body></html>"""


def doctor_page(index: Index, identifier: str) -> str:
    """Why an identifier is or is not on the surface (§3.9).

    Renders the whole chain, with the FIRST broken link carrying the remedy.
    Showing every failure at once buries the one that caused the others.
    """
    from ..diagnose import doctor

    d = doctor(index, identifier)
    rows = []
    shown_remedy = False
    for step in d.steps:
        mark = "ok" if step.ok else "FAIL"
        css = "state-HEALTHY" if step.ok else "state-FAILED"
        remedy = ""
        if not step.ok and step.remedy and not shown_remedy:
            remedy = f'<br><small>&rarr; {esc(step.remedy)}</small>'
            shown_remedy = True
        rows.append(
            f'<tr class="{css}"><td>{esc(step.name)}</td><td>{mark}</td>'
            f"<td>{esc(step.detail)}{remedy}</td></tr>"
        )
    body = "".join(rows) or (
        '<tr><td colspan="3" class="absent">nothing knows this identifier</td></tr>'
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>doctor · {esc(identifier)}</title></head><body>
<nav><a href="/">fleet</a> &rsaquo; doctor</nav>
<h1>doctor: {esc(identifier)}</h1>
{index_freshness(index)}
<p class="{"state-HEALTHY" if d.ok else "state-FAILED"}">{esc(d.summary())}</p>
<table><thead><tr><th>link</th><th></th><th>detail</th></tr></thead>
<tbody>{body}</tbody></table>
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
