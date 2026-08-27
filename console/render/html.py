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
from ..model.kinds import EXCEPTION_VALUES, STATE_FILTER, Kind, State
from ..server.router import path_for_entity, path_for_list

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
    thirteen, and a raw value (an Artifact's freshness, a tracker's open/closed)
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
    # One filter implementation, shared with the JSON representation (§3.8:
    # the same query, both renderings). The hand-rolled loop this replaces
    # matched `e.facets` ONLY, so the two representations of the same URL
    # already disagreed on baseline-comparison filters — and would have
    # disagreed again on `state=` (alpha-engine-config-I7107).
    from .json import filter_entities

    entities = filter_entities(index.of_kind(kind), facets)
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
    if info.build_seconds is not None:
        cadence = f"{cadence}; last build {info.build_seconds:.1f}s"
        if info.cadence_overrun:
            cadence = f"{cadence} (exceeds cadence)"
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
    (the decision queue) · the completeness ratio. No aggregate green light.

    Above the exception table, and only when configuration declares one, §4.4's
    milestone pane: the declared exit predicate, clause by clause. It sits
    there because "is the thing we are building finished" is the question a
    reader brings to this page second, immediately after "is anything on fire",
    and it was previously answerable only by hand off five other surfaces.
    """
    exceptions = [e for e in index.all() if is_exception(e)]
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
        else "unknown — no registry configured, or a declared registry could "
             "not be read (§9.1)"
    )
    # The members, LINKED — §3.1's structure path. `_format_number` can only
    # emit names (its output is escaped by `_number_row`), so the one place a
    # reader can click through from §9.1's count to the rows behind it is here
    # (alpha-engine-config-I7107).
    unregistered_links = _member_links(
        Kind.COMPONENT, State.UNREGISTERED.value,
        completeness.get("unregistered_ids") or ())
    gap = index.transparency_gap()
    from .json import numbers as _numbers
    n = _numbers(index, exceptions, conflicts, gap)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>fleet</title></head><body>
<h1>fleet — exceptions</h1>
<form action="/search" method="get"><label for="global-search">search fleet</label> <input id="global-search" name="q" accesskey="/" autocomplete="off"><button type="submit">search</button></form>
{index_freshness(index)}
<h2>registries</h2><ul>{''.join(f'<li><a href="/registry/{esc(name)}">{esc(name)}</a></li>' for name in index.registry_names()) or '<li class="absent">none declared</li>'}</ul>
<p>registry pages {esc(registry_txt)}{missing} · {len(exceptions)} not healthy · {gap["count"]} / {gap["of"]} unreported (transparency gap, §9.2) · {len(conflicts)} claim conflicts · index reachability {esc(ratio_txt)}</p>
{milestones_section(index, n)}
{_table(exceptions)}
<h2>waiting on Brian</h2>
{_table(queue)}
<p>population completeness {esc(completeness_txt)} · {completeness["unregistered"]} unregistered (§9.1){unregistered_links}</p>
{numbers_section(index, exceptions, conflicts, gap, n)}
</body></html>"""


def _member_links(kind: Kind, state: str, member_ids) -> str:
    """The rows behind a §9 count, as links — the count's evidence field (§5.1).

    Renders the filtered list URL (`/<kind>?state=<STATE>`) alongside each
    member's own entity page, so the members are navigable by structure and not
    merely enumerable in prose. Empty string when there are none: a zero count
    has nothing to link to, and an empty "see:" is noise on a healthy surface.
    """
    members = list(member_ids)
    if not members:
        return ""
    listing = path_for_list(kind, {STATE_FILTER: state})
    links = " · ".join(
        f'<a href="{esc(path_for_entity(kind, mid))}">{esc(mid)}</a>'
        for mid in members
    )
    return (f' — <a href="{esc(listing)}">all {esc(state)} {esc(kind.value)}s</a>'
            f': {links}')


#: The question this pane answers, rendered ON the pane (§4.4). Held beside the
#: renderer so the pane registry entry and the heading cannot drift: a pane
#: whose declared question and rendered question differ is two panes.
_MILESTONE_PANE_QUESTION = (
    "has the declared milestone been met, and which clause is holding it"
)


def milestones_section(index: Index, numbers: dict) -> str:
    """console-policy.md §4.4's milestone pane — declared predicates, evaluated.

    Renders nothing at all when no milestone is declared. That is not §5.5's
    forbidden blank region: §5.5 governs a fact this surface is EXPECTED to
    carry and could not read, and a deployment that declares no milestone is
    not expecting one. A pane that rendered "no milestones" on every console
    that has none would be the aggregate green light §4.3 forbids, wearing an
    empty state as a disguise.

    Every clause carries §5.1's four fields plus the target it is measured
    against, and an UNREPORTED clause carries the reason it could not be read —
    never a blank cell, and never counted toward `met`.
    """
    from ..index.milestones import MET, UNREPORTED, evaluate

    declared = evaluate(index, numbers)
    if not declared:
        return ""
    return "".join(_milestone(m) for m in declared)


def _milestone(m: dict) -> str:
    tracker = (
        f' &middot; <a href="{esc(m["tracker"])}">tracker</a>'
        if m.get("tracker") else
        ' &middot; <em class="absent">no tracker link declared</em>'
    )
    # `N of M clauses met`, never a single verdict: §4.3 forbids the aggregate
    # green light, and the unreported count is stated SEPARATELY because a
    # clause nobody could read is not a clause that failed.
    unreported = (
        f' &middot; <span class="state-UNREPORTED">{m["unreported"]} UNREPORTED</span>'
        if m.get("unreported") else ""
    )
    holding = (
        " &middot; holding: " + ", ".join(esc(c) for c in m["holding"])
        if m.get("holding") else
        " &middot; no clause outstanding"
    )
    rows = "".join(_milestone_rows(c) for c in m["clauses"])
    return (
        f'<h2>milestone: {esc(m["id"])}</h2>'
        # §4.4: the question sentence is rendered on the pane, not left in a
        # registry a reader never opens.
        f'<p class="pane-question">{esc(_MILESTONE_PANE_QUESTION)} &mdash; '
        f'{esc(m["question"])}</p>'
        f'<p>{m["met"]} of {m["of"]} clauses met{unreported}{holding}{tracker}</p>'
        "<table><thead><tr><th>clause</th><th>status</th><th>bound to</th>"
        "<th>value</th><th>target</th><th>as-of</th><th>evidence</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _milestone_rows(clause: dict) -> str:
    """One row per TERM, each repeating its clause id and status.

    Repeated rather than row-spanned on purpose: a blank cell under a spanned
    heading is indistinguishable from a fact this pane could not supply, which
    is the confusion §5.5 exists to remove.
    """
    if not clause["terms"]:
        # A clause whose PRECONDITION refused — it has no terms to show, and
        # the reason is the whole finding.
        return (
            f'<tr class="milestone-{esc(clause["status"])}">'
            f'<td>{esc(clause["id"])}<br><small>{esc(clause["label"])}</small></td>'
            f'<td>{esc(clause["status"])}</td>'
            f'<td colspan="5"><em class="absent">{esc(clause.get("reason", "no reason given"))}</em></td>'
            "</tr>"
        )
    return "".join(_milestone_term_row(clause, t) for t in clause["terms"])


def _milestone_term_row(clause: dict, term: dict) -> str:
    as_of = (
        esc(term["as_of"]) if term.get("as_of")
        else '<em class="absent">no freshness stamp</em>'
    )
    evidence = (
        f'<a href="{esc(term["evidence"])}">evidence</a>' if term.get("evidence")
        else '<em class="absent">no link</em>'
    )
    value = (
        esc(term["value"]) if term.get("value") is not None
        else '<em class="absent">no value</em>'
    )
    reason = (
        f'<br><small class="absent">{esc(term["reason"])}</small>'
        if term.get("reason") else ""
    )
    return (
        # `milestone-` and not `state-`: MET/UNMET are not members of
        # observability-policy.md §8.3's closed state vocabulary, and
        # borrowing its selector namespace for two values that are not
        # states is how a vocabulary stops being closed. UNREPORTED IS one
        # of the thirteen and means here exactly what it means there.
        f'<tr class="milestone-{esc(clause["status"])}">'
        f'<td>{esc(clause["id"])}<br><small>{esc(clause["label"])}</small></td>'
        f'<td>{esc(term["status"])}</td>'
        f'<td><code>{esc(term["binding"])}:{esc(term["ref"])}.{esc(term["selector"])}</code>{reason}</td>'
        f"<td>{value}</td>"
        f'<td>{esc(term["op"])} {esc(term["target"])}</td>'
        f"<td>{as_of}</td><td>{evidence}</td>"
        "</tr>"
    )


def numbers_section(index: Index, exceptions: list[Entity], conflicts: list[Entity],
                    gap: dict, numbers: dict | None = None) -> str:
    """console-policy.md §9 — the nine numbers, rendered (§3.8: the JSON
    representation carries the identical shape via `render.json.numbers`,
    which this function reads rather than recomputing).

    `numbers` is the already-assembled dict when the caller has one — the
    landing view grades its milestone clauses (§4.4) against the same numbers
    it renders here, and computing them twice per page would let one section
    render a value the other did not use.
    """
    from .json import numbers as _numbers

    n = numbers if numbers is not None else _numbers(index, exceptions, conflicts, gap)
    rows = "".join(_number_row(label, key, n[key]) for label, key in _NUMBER_ROWS)
    return f"""<h2>the nine numbers</h2>
<table><thead><tr><th>§</th><th>number</th><th>value</th></tr></thead>
<tbody>{rows}</tbody></table>"""


#: §9's numbered order, matching console-policy.md §9's own enumeration —
#: §9.7 is rendered by `_SURFACE_LIVENESS_NOT_IMPL` above, in step with the
#: others rather than as a special case.
_NUMBER_ROWS = (
    ("§9.1 population completeness", "population_completeness"),
    ("§9.2 transparency gap", "transparency_gap"),
    ("§9.3 index reachability", "index_reachability"),
    ("§9.4 answer latency", "answer_latency"),
    ("§9.5 orphan count", "orphan_count"),
    ("§9.6 staleness honesty", "staleness_honesty"),
    ("§9.7 surface liveness", "surface_liveness"),
    ("§9.8 onboarding cost", "onboarding_cost"),
    ("§9.9 claim conflicts", "claim_conflicts"),
)


def _number_row(label: str, key: str, value: object) -> str:
    return (f'<tr class="number-{esc(key)}"><td>{esc(label)}</td>'
            f"<td>{esc(key)}</td><td>{esc(_format_number(value))}</td></tr>")


def _format_number(value: object) -> str:
    """A compact, honest one-line rendering of any §9 number's shape.

    Never invents a ratio: `state: N/A-NOT-IMPL` and `computable: False` both
    render their stated reason rather than a number, matching §5.4 — a figure
    with no baseline (or no VALUE at all) is telemetry, never a coloured verdict.
    """
    if not isinstance(value, dict):
        return str(value)
    if value.get("state") == "N/A-NOT-IMPL":
        return f'N/A-NOT-IMPL (expected: {value.get("expected_cycle", "unstated")})'
    if value.get("computable") is False:
        return f'not computable — {value.get("reason", "no reason given")}'
    if "count" in value and "of" in value:
        base = f'{value["count"]} / {value["of"]}'
        # A number that NAMES its members renders them (§5.1's evidence field).
        # §9.6's members appear on no other view by construction — a staleness
        # violation is a row whose state is not in EXCEPTION_STATES — so the
        # count alone is a finding nobody can act on.
        members = value.get("violations")
        if members:
            # Not escaped here: every caller passes this through `esc` (see
            # `_number_row`), and escaping twice renders the entities literally.
            base = f'{base} — {", ".join(str(m) for m in members)}'
        # A row EXCLUDED from the denominator is invisible in `count / of` by
        # construction, and an unexplained shrinking denominator is the defect
        # this number exists to catch happening to the number itself
        # (alpha-engine-config-I7126). Named, not silently dropped.
        excluded = value.get("unauditable")
        if excluded:
            base += (f' · {len(excluded)} unauditable: '
                     + ", ".join(sorted(str(k) for k in excluded)))
        return base
    if {"pane_orphans", "kind_orphans"} <= value.keys():
        po, ko = value["pane_orphans"], value["kind_orphans"]
        return f'panes {po["count"]}/{po["of"]} · kinds {ko["count"]}/{ko["of"]}'
    if "ratio" in value:
        of = value.get("of")
        rendered = value.get("rendered", 0)
        unreg = value.get("unregistered")
        tail = f' · {unreg} unregistered' if unreg is not None else ''
        # §9.1 names its members for the same reason §9.6 does: an
        # UNREGISTERED row is one line in an exception table 100+ rows long,
        # so the count moving 0 -> 1 was unattributable from this surface
        # (alpha-engine-config-I7107). Same escaping contract as §9.6 above.
        for key, label in (("unregistered_ids", "unregistered"),
                           ("unrendered_ids", "declared but not rendered")):
            members = value.get(key)
            if members:
                tail += (f' · {label}: '
                         f'{", ".join(str(m) for m in members)}')
        if of is None:
            return f'unknown — no registry configured or unreadable (§9.1){tail}'
        return f'{rendered} / {of}{tail}'
    if "total" in value:  # reachability's own shape
        return f'{value.get("reachable_all_three", 0)} / {value["total"]}'
    if "of" in value and "applicable" in value:  # answer latency
        return (f'{value["within_budget"]} / {value["applicable"]} within budget'
                f' (v{value.get("version")})')
    return str(value)


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
