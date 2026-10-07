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
from ..model.entity import DOCUMENT_DETAIL, DOCUMENT_MARKDOWN, LABEL_DETAIL, Entity
from ..model.fields import Field, format_value, parse as parse_fields, part_of_whole
from ..index.numbers import artifact_observation_coverage
from ..model.kinds import (
    EXCEPTION_VALUES,
    STATE_FILTER,
    UNOBSERVED_VALUE,
    Kind,
    State,
)
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


def _is_component_echo(ent: Entity, index: Index) -> bool:
    """Whether a RUN row says nothing the landing view doesn't already say.

    A check envelope mints one Component and one Run from the same publish
    (`adapters/checks_envelope.py`), and when the component's state was
    derived from this exact run (§4.3, alpha-engine-config-I8979) the run adds
    no information — same verdict, same `ran_at`, reachable from the
    component by the `belongs-to` relation already. A run that disagrees with
    its component — a transient failure under an otherwise HEALTHY component
    — is a DIFFERENT fact and still lists.
    """
    if ent.kind is not Kind.RUN:
        return False
    for edge in index.related(ent.id):
        if edge.source != ent.id or edge.rel != "belongs-to":
            continue
        component = index.entity(edge.target)
        if (
            component is not None
            and component.kind is Kind.COMPONENT
            and component.state == ent.state
            and component.provenance.as_of == ent.provenance.as_of
        ):
            return True
    return False


def landing_exceptions(index: Index) -> list[Entity]:
    """The §4.3 exception list: every non-HEALTHY row, minus a run that only
    echoes the component derived from it (alpha-engine-config-I8979). The
    component row stays; its latest run is reachable from it by relation."""
    return [
        e for e in index.all()
        if is_exception(e) and not _is_component_echo(e, index)
    ]


def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def row(ent: Entity, columns: list[str] | None = None, label: str | None = None) -> str:
    """One row: id, state, then any declared field columns, then the rest of
    §5.1's four fields (source · as-of · evidence).

    ``columns`` names the declared fields a filtered list such as a dashboard
    shows, one column each, so its rows read without a click per row. Values
    are rendered by `format_value` alone, the same path as the entity page
    (§5.8): nothing here knows who emitted a field.
    """
    p = ent.provenance
    as_of = esc(p.as_of) if p.as_of else '<em class="absent">no freshness stamp</em>'
    evidence = (
        f'<a href="{esc(p.evidence)}">evidence</a>' if p.evidence
        else '<em class="absent">no link</em>'
    )
    return (
        f'<tr class="state-{esc(ent.state_value)}">'
        f'<td><a href="{esc(path_for_entity(ent.kind, ent.id))}">{esc(label or ent.id)}</a></td>'
        f"<td>{esc(ent.state_value)}</td>"
        + "".join(f"<td>{cell}</td>" for cell in _field_cells(ent, columns or []))
        + f"<td>{esc(p.source)}</td>"
        f"<td>{as_of}</td>"
        f"<td>{evidence}</td>"
        "</tr>"
    )


def _field_cells(ent: Entity, columns: list[str]) -> list[str]:
    by_name = {f.name: f for f in parse_fields(ent.detail.get("fields"))}
    out = []
    for name in columns:
        f = by_name.get(name)
        if f is None:
            out.append('<em class="absent">—</em>')
        else:
            out.append(_value_html(f, by_name))
    return out


def _value_html(f: Field, by_name: dict[str, Field], with_unit: bool = True) -> str:
    """A field's rendered value; a part-of-whole field adds a bar (I11805).

    The bar is a native `<progress>` capped at the whole, with the numbers and
    percentage beside it in text, so nothing is carried by length alone
    (§5.7). It is uncoloured: `of` declares a relation, not a baseline, and
    §5.4 colours only a declared baseline.
    """
    unit = f" {esc(f.unit)}" if f.unit and with_unit else ""
    pair = part_of_whole(f, by_name.get(f.of or ""))
    if pair is None:
        return esc(format_value(f)) + unit
    value, whole = pair
    whole_txt = esc(format_value(by_name[f.of]))  # type: ignore[index]
    if whole <= 0:
        pct_txt = "no budget" if value <= 0 else "over a zero budget"
        bar_value, bar_max = (1, 1) if value > 0 else (0, 1)
    else:
        pct = value / whole
        pct_txt = f"{pct:.0%}" + (" — over" if pct > 1 else "")
        bar_value, bar_max = min(value, whole), whole
    return (f'<progress value="{bar_value:g}" max="{bar_max:g}"></progress> '
            f"{esc(format_value(f))} / {whole_txt}{unit} ({esc(pct_txt)})")


def _field_columns(entities: list[Entity]) -> list[str]:
    """The declared fields a list shows as columns, in DECLARATION order.

    Declaration order is the author's priority (a SEV and a summary first, the
    detail after), so the union of every row's names keeps first-seen order
    rather than the alphabetical order `parse` keeps for the entity page.
    `question` is the pane's own question, the same on every row, so it stays
    on the entity page rather than taking a column.
    """
    seen: dict[str, None] = {}
    for e in entities:
        raw = e.detail.get("fields")
        if isinstance(raw, dict):
            for n in raw:
                if str(n) != "question":
                    seen.setdefault(str(n), None)
    return list(seen)


def _table(entities: list[Entity], with_fields: bool = False,
           labels: dict[str, str] | None = None) -> str:
    if not entities:
        # Absence renders as itself — an empty state is a rendered fact, not
        # a blank region (§5.5).
        return '<p class="absent">No entities — the source reported none.</p>'
    columns = _field_columns(entities) if with_fields else []
    labels = labels or {}
    rows = "".join(row(e, columns, labels.get(e.id)) for e in entities)
    return (
        "<table><thead><tr>"
        "<th>id</th><th>state</th>"
        + "".join(f"<th>{esc(c)}</th>" for c in columns)
        + "<th>source</th><th>as-of</th><th>evidence</th>"
        + f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _grouped_tables(entities: list[Entity],
                    labels: dict[str, str] | None = None) -> str:
    """One table per source when the rows come from more than one (I11805).

    A dashboard is a pane filter, so it can gather rows from several sources
    that declare different fields (spend per provider, CI minutes, the Claude
    plan). One table over all of them is the union of every source's columns,
    and most cells read "—". A table per source shows only that source's own
    fields. Order is kept: groups follow the first row of each, and the list is
    already not-healthy first, so the group needing attention leads.
    """
    if not any(e.detail.get("fields") for e in entities):
        return _table(entities, labels=labels)
    groups: dict[str, list[Entity]] = {}
    for e in entities:
        groups.setdefault(e.provenance.source or "", []).append(e)
    if len(groups) == 1:
        return _table(entities, with_fields=True, labels=labels)
    parts = []
    for source, rows in groups.items():
        not_healthy = sum(1 for e in rows if is_exception(e))
        note = f" · {not_healthy} not healthy" if not_healthy else ""
        heading = _group_question(rows) or source or "no source"
        parts.append(
            f'<h2 class="list-group">{esc(heading)}</h2>'
            f'<p class="list-summary">{len(rows)} rows{note} · {esc(source)}</p>'
            + _table(rows, with_fields=any(e.detail.get("fields") for e in rows),
                     labels=labels)
        )
    return "\n".join(parts)


def _group_question(rows: list[Entity]) -> str | None:
    """The question a group's rows answer, when they all declare the same one.

    A pane fragment stamps its question on every row it emits (§4.4), so a
    group from one fragment can be headed by what it answers rather than by an
    artifact path.
    """
    questions = set()
    for e in rows:
        raw = e.detail.get("fields")
        q = raw.get("question") if isinstance(raw, dict) else None
        if isinstance(q, dict):
            q = q.get("value")
        questions.add(q if isinstance(q, str) and q else None)
    return questions.pop() if len(questions) == 1 else None


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
    by_name = {f.name: f for f in declared}
    rows = "".join(_field_row(f, by_name) for f in declared)
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


def _field_row(f: Field, by_name: dict[str, Field] | None = None) -> str:
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
        f"<td>{_value_html(f, by_name or {}, with_unit=False)}</td>"
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
    # A declared label heads the page; the id stays in the breadcrumb and the
    # row below it, so the identifier is never hidden by the name.
    label = ent.detail.get(LABEL_DETAIL)
    heading = label if isinstance(label, str) and label else ent.id
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{esc(heading)} · {esc(ent.kind.value)}</title></head><body>
<nav><a href="/">fleet</a> &rsaquo; <a href="/{esc(ent.kind.route)}">{esc(ent.kind.value)}</a> &rsaquo; {esc(ent.id)}</nav>
<h1>{esc(heading)}</h1>
{index_freshness(index)}
<p class="state-{esc(ent.state_value)}">state: {esc(ent.state_value)}</p>
{_table([ent])}
{document_section(ent)}
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


def list_page(index: Index, kind: Kind, facets: dict[str, str], page: int = 1,
              all_runs: bool = False, days: int | None = None,
              days_all: bool = False) -> str:
    """A filtered list — the facets are in the URL, so this reproduces cold (§3.4)."""
    # One filter-collapse-order implementation, shared with the JSON
    # representation (§3.8: the same query, both renderings) —
    # `render.json.list_rows`, alpha-engine-config-I7107 / I11805.
    from ..server.router import ALL_DAYS, DAYS_PARAM, RUNS_PARAM, Resolved
    from .json import list_rows

    listing = list_rows(index, Resolved(view="list", kind=kind, facets=facets,
                                        page=page, all_runs=all_runs,
                                        days=days, days_all=days_all))
    entities = listing.rows
    start = (page - 1) * PAGE_SIZE
    visible = entities[start:start + PAGE_SIZE]
    shown = (f"<p>showing {len(visible)} of {listing.total} · "
             f"{len(entities)} filtered · page {page}</p>")
    summary = " · ".join(
        f'<span class="state-{esc(state)}">{n} {esc(state)}</span>'
        for state, n in listing.summary.items()
    ) or '<span class="absent">no rows</span>'
    noun = "jobs" if listing.collapsed else f"{kind.value}s"
    summary_line = f'<p class="list-summary">{len(entities)} {noun} · {summary}</p>'
    toggle = ""
    url_state = dict(facets)
    if listing.dated:
        toggle = _window_line(kind, facets, listing)
        if days_all:
            url_state[DAYS_PARAM] = ALL_DAYS
        elif days:
            url_state[DAYS_PARAM] = str(days)
    elif kind is Kind.RUN:
        if listing.collapsed:
            toggle = (f'<p>newest run per job — {len(entities)} jobs from '
                      f'{listing.filtered_runs} runs · <a href="'
                      f'{esc(path_for_list(kind, {**facets, RUNS_PARAM: "all"}))}">'
                      "show every run</a></p>")
        else:
            url_state[RUNS_PARAM] = "all"
            toggle = (f'<p>every run · <a href="{esc(path_for_list(kind, facets))}">'
                      "show newest run per job</a></p>")
    title = f"{kind.value}s"
    table = _grouped_tables(visible, labels=listing.labels)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{esc(title)}</title></head><body>
<nav><a href="/">fleet</a> &rsaquo; {esc(title)}</nav>
<h1>{esc(title)}</h1>
{index_freshness(index)}{summary_line}{toggle}{shown}
{table}
{_pager(kind, url_state, page, len(entities))}
</body></html>"""


#: Rows per list page, the same slice the JSON representation takes.
PAGE_SIZE = 50


def _window_line(kind: Kind, facets: dict[str, str], listing) -> str:
    """What a dated list shows, and the one link to the other view (§3.4).

    The window view links to the archive of every row; the archive links back
    to the window the rows declare. Both are plain URLs, so either can be
    pasted into an issue and reproduces cold (§3.2).
    """
    from ..server.router import ALL_DAYS, DAYS_PARAM

    noun = f"{kind.value}s"
    if listing.window_days is not None:
        archive = path_for_list(kind, {**facets, DAYS_PARAM: ALL_DAYS})
        outside = (f" · {listing.outside} older or undated not shown"
                   if listing.outside else "")
        return (f'<p class="list-window">past {listing.window_days} days, newest first'
                f' — {len(listing.rows)} of {listing.filtered_runs} {esc(noun)}{outside}'
                f' · <a href="{esc(archive)}">archive: every {esc(kind.value)} '
                f'({listing.filtered_runs})</a></p>')
    back = path_for_list(kind, facets)
    back_text = (f"past {listing.declared_window} days" if listing.declared_window
                 else "default view")
    return (f'<p class="list-window">archive: every {esc(kind.value)}, newest first'
            f' — {len(listing.rows)} {esc(noun)} · <a href="{esc(back)}">'
            f'{esc(back_text)}</a></p>')


def _pager(kind: Kind, url_state: dict[str, str], page: int, rows: int) -> str:
    """Previous/next links when a list runs past one page, so every row is
    reachable by following links rather than by editing `?page=` by hand."""
    pages = max(1, -(-rows // PAGE_SIZE))
    if pages == 1:
        return ""
    links = []
    if page > 1:
        prev_url = path_for_list(kind, {**url_state, "page": str(page - 1)})
        links.append(f'<a href="{esc(prev_url)}">previous page</a>')
    if page < pages:
        next_url = path_for_list(kind, {**url_state, "page": str(page + 1)})
        links.append(f'<a href="{esc(next_url)}">next page</a>')
    return f'<p class="pager">page {page} of {pages} · ' + " · ".join(links) + "</p>"


def document_section(ent: Entity) -> str:
    """A source object's own text, shown where it is listed (`detail["document"]`).

    A plain-text document is preformatted and escaped: the console shows it as
    text and never interprets it, so a report cannot inject markup into the
    page. A document the source declares as markdown (`"format": "markdown"`)
    is rendered by `render.markdown`, which has raw HTML off and refuses every
    link scheme but http(s); without a renderer installed it falls back to the
    escaped text and says so. A cut document says so, and how large the whole
    object is.
    """
    from . import markdown

    doc = ent.detail.get(DOCUMENT_DETAIL)
    if not isinstance(doc, dict) or not isinstance(doc.get("text"), str):
        return ""
    note = ""
    if doc.get("truncated"):
        note = (f'<p class="absent">document cut — showing the first part of '
                f'{esc(doc.get("bytes"))} bytes; the whole object is at the evidence link</p>')
    if doc.get("format") == DOCUMENT_MARKDOWN:
        rendered = markdown.render(doc["text"])
        if rendered is not None:
            return f'<h2>document</h2>{note}<div class="document markdown">{rendered}</div>'
        note += ('<p class="absent">markdown renderer unavailable — '
                 'showing the document as text</p>')
    return f'<h2>document</h2>{note}<pre class="document">{esc(doc["text"])}</pre>'


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
            + (" (bootstrap window)" if info.bootstrap else "")
            + f". {esc(cadence)}. sources: {esc(sources)}</p>"
        )
    return (
        f'<p class="index-fresh">index built {esc(info.built_at)} · '
        f"{esc(cadence)} · {esc(info.staleness_basis())} · "
        f"sources: {esc(sources)}</p>"
    )


#: The order the landing view's exception groups render in: the states that
#: mean something broke first, then the ones that mean nobody can tell (I11805).
_SEVERITY_ORDER = (
    State.FAILED, State.STALLED, State.MISSED, State.NEVER_RAN,
    State.DEGRADED, State.UNREPORTED, State.ABSENT, State.UNREGISTERED,
)
#: Groups that open by default. The rest render collapsed, still in the page,
#: so a reader sees what broke before scrolling past what is merely unreported.
_OPEN_STATES = frozenset({State.FAILED, State.STALLED, State.MISSED, State.NEVER_RAN})


def exceptions_by_state(exceptions: list[Entity]) -> str:
    """The §4.3 exception list, grouped by state, worst first (I11805).

    The same rows as one flat table, newest first within a group; nothing is
    filtered. A flat list of a few hundred rows in id order put a stale probe
    from August above today's failure, so the reader could not tell what to
    look at. A count line links to each group.
    """
    if not exceptions:
        return '<h2>not healthy</h2><p class="absent">No exceptions — every row is HEALTHY.</p>'
    groups: dict[str, list[Entity]] = {}
    newest_first = sorted(exceptions, key=lambda e: e.provenance.as_of or "", reverse=True)
    for e in newest_first:
        groups.setdefault(e.state_value, []).append(e)
    known = [s.value for s in _SEVERITY_ORDER]
    order = [v for v in known if v in groups] + sorted(v for v in groups if v not in known)
    counts = " · ".join(
        f'<a class="state-{esc(v)}" href="#not-healthy-{esc(v)}">{len(groups[v])} {esc(v)}</a>'
        for v in order
    )
    open_values = {s.value for s in _OPEN_STATES}
    sections = "".join(
        f'<details id="not-healthy-{esc(v)}"{" open" if v in open_values else ""}>'
        f'<summary class="state-{esc(v)}">{esc(v)} · {len(groups[v])}</summary>'
        f"{_table(groups[v])}</details>"
        for v in order
    )
    return (f"<h2>not healthy</h2>"
            f'<p class="list-summary">{len(exceptions)} rows · {counts}</p>{sections}')


def dashboard_links(dashboards: list[dict]) -> str:
    """One line of links to every dashboard, at the top of the landing view.

    Navigation, not tiles (§4.3): the landing view stays the exception list,
    and this line saves the reader a click to /dashboards for the one they
    came for. Generated from the same `pane` facets as /dashboards (§3.5).
    """
    if not dashboards:
        return ""
    links = " · ".join(
        f'<a href="{esc(d["url"])}">{esc(d["pane"])}</a>'
        + (f' <span class="state-DEGRADED">({d["not_healthy"]} not healthy)</span>'
           if d["not_healthy"] else "")
        for d in dashboards
    )
    return f'<p class="dashboard-links">dashboards: {links} · <a href="/dashboards">all</a></p>'


def landing_page(index: Index) -> str:
    """The exception-first default view (§4.3): what is not HEALTHY, with
    state and age · the transparency-gap count · what is waiting on Brian
    (the decision queue) · the completeness ratio. No aggregate green light.

    Above the exception table, and only when configuration declares one, §4.4's
    milestone pane: the declared exit predicate, clause by clause. It sits
    there because "is the thing we are building finished" is the question a
    reader brings to this page second, immediately after "is anything on fire",
    and it was previously answerable only by hand off five other surfaces.

    Every fact rendered below is read from `index.landing_model()` — computed
    once per `Index`, never recomputed here (alpha-engine-config-I10615). The
    JSON representation of this same URL (`render.json._landing`) reads the
    identical model, so the two representations cannot disagree about what
    they were computed from, only about how they render it.
    """
    model = index.landing_model()
    exceptions = model.exceptions
    conflicts = model.conflicts
    reach = model.reachability
    ratio = reach["ratio"]
    ratio_txt = (
        f'{reach["reachable_all_three"]} / {reach["total"]}'
        if ratio is not None else "no entities yet"
    )
    registries = model.registries
    registry_txt = f'{registries["count"]} / {registries["of"]}'
    missing = (f' · missing: {esc(", ".join(registries["missing"]))}'
               if registries["missing"] else "")
    queue = model.queue
    completeness = model.completeness
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
    gap = model.gap
    gap_txt = (
        f'{gap["count"]} / {gap["of"]} unreported (transparency gap, §9.2)'
        if gap.get("computable", True) is not False
        else f'transparency gap not computable — '
             f'{esc(gap.get("reason", "no reason given"))}'
    )
    n = model.numbers
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>fleet</title></head><body>
<h1>fleet — exceptions</h1>
{dashboard_links(model.dashboards)}
<form action="/search" method="get"><label for="global-search">search fleet</label> <input id="global-search" name="q" accesskey="/" autocomplete="off"><button type="submit">search</button></form>
{index_freshness(index)}
<h2>registries</h2><ul>{''.join(f'<li><a href="/registry/{esc(name)}">{esc(name)}</a></li>' for name in index.registry_names()) or '<li class="absent">none declared</li>'}</ul>
<p>registry pages {esc(registry_txt)}{missing} · {len(exceptions)} not healthy · {gap_txt} · {len(conflicts)} claim conflicts · index reachability {esc(ratio_txt)}</p>
{milestones_section(model.milestones, model.milestone_journal)}
{exceptions_by_state(exceptions)}
<h2>waiting on Brian</h2>
{_table(queue)}
<p>population completeness {esc(completeness_txt)} · {completeness["unregistered"]} unregistered (§9.1){unregistered_links}</p>
{observation_coverage_line(index)}
{numbers_section(index, exceptions, conflicts, gap, n)}
</body></html>"""


#: The question the dashboards pane answers, rendered on it (§4.4) and held
#: beside the renderer so the registry entry and the heading cannot drift.
_DASHBOARDS_PANE_QUESTION = (
    "which dashboards and lists exist, and what state is each in"
)


def dashboards_page(index: Index) -> str:
    """The navigation index: every dashboard, milestone, registry and list.

    console-policy.md §3.1 requires a navigation path to every entity; a
    dashboard reachable only by typing its filtered URL is §3.1's hidden URL.
    Everything here is derived from the index on each build (§3.5) — a
    dashboard is a `pane` facet value on its rows, so a new one appears here
    with no edit to the console. The landing view stays the exception list
    (§4.3); this page is one click from every page via `with_site_nav`.
    """
    model = index.landing_model()
    dashboards = "".join(
        f'<li><a href="{esc(d["url"])}">{esc(d["pane"])}</a> · '
        f'{d["rows"]} {esc(d["kind"])} rows · '
        + (f'<span class="state-DEGRADED">{d["not_healthy"]} not healthy</span>'
           if d["not_healthy"] else "all healthy")
        + "</li>"
        for d in model.dashboards
    ) or '<li class="absent">none declared — no row carries a pane facet</li>'
    milestones = "".join(
        f'<li><a href="/#milestone-{esc(m["id"])}">{esc(m["id"])}</a> · '
        f'{m["met"]} of {m["of"]} clauses met'
        + (f' · <span class="state-UNREPORTED">{m["unreported"]} UNREPORTED</span>'
           if m.get("unreported") else "")
        + "</li>"
        for m in model.milestones
    ) or '<li class="absent">none declared</li>'
    registries = "".join(
        f'<li><a href="/registry/{esc(name)}">{esc(name)}</a></li>'
        for name in index.registry_names()
    ) or '<li class="absent">none declared</li>'
    lists = "".join(
        f'<li><a href="/{esc(k.route)}">{esc(k.value)}s</a> · '
        f'{len(index.of_kind(k))} rows</li>'
        for k in Kind
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>dashboards</title></head><body>
<nav><a href="/">fleet</a> &rsaquo; dashboards</nav>
<h1>dashboards</h1>
<p class="pane-question">{esc(_DASHBOARDS_PANE_QUESTION)}</p>
{index_freshness(index)}
<h2>dashboards</h2><ul>{dashboards}</ul>
<h2>milestones</h2><ul>{milestones}</ul>
<h2>registries</h2><ul>{registries}</ul>
<h2>all lists</h2><ul>{lists}</ul>
</body></html>"""


def _schedule_word(state: str) -> str:
    """The state word, always rendered beside its colour (§5.7)."""
    return f'<span class="schedule-word schedule-{esc(state)}">{esc(state)}</span>'


def _planner_axis_pos(day: str, start, span_days: int) -> float:
    from datetime import date as _date

    offset = (_date.fromisoformat(day) - start).days
    return max(0.0, min(100.0, 100.0 * offset / span_days)) if span_days else 0.0


def _planner_timeline(projects: list[dict]) -> str:
    """Every project as one row on one dated axis; each milestone a bar from
    its start to its target, labelled with its own state. No project colour:
    the row's colours are its milestones' (§4.3)."""
    from datetime import date as _date, datetime as _dt, timezone as _tz
    from ..index import planner

    span = planner.axis(projects)
    if span is None:
        return '<p class="absent">no milestones declared</p>'
    from datetime import timedelta as _pad

    # A few days of margin past the last target, so its diamond and label
    # are not clipped by the edge of the track.
    start, end = span[0], span[1] + _pad(days=3)
    days = max(1, (end - start).days)
    today = _dt.now(_tz.utc).date()
    from datetime import timedelta as _td

    ticks = []
    # Weekly (Monday) ticks under the month ticks, so a target's week reads
    # off the axis without hovering.
    monday = start + _td(days=(7 - start.weekday()) % 7)
    while monday <= end:
        if monday.day > 3 and _planner_axis_pos(monday.isoformat(), start, days) < 95:
            ticks.append(f'<span class="plan-tick plan-week" style="left:'
                         f'{_planner_axis_pos(monday.isoformat(), start, days):.2f}%">'
                         f'{monday.strftime("%b")} {monday.day}</span>')
        monday += _td(days=7)
    month = _date(start.year, start.month, 1)
    while month <= end:
        if month >= start:
            ticks.append(f'<span class="plan-tick" style="left:'
                         f'{_planner_axis_pos(month.isoformat(), start, days):.2f}%">'
                         f'{month.strftime("%b %d")}</span>')
        month = (_date(month.year + 1, 1, 1) if month.month == 12
                 else _date(month.year, month.month + 1, 1))
    today_line = ""
    if start <= today <= end:
        today_line = (f'<span class="plan-today" style="left:'
                      f'{_planner_axis_pos(today.isoformat(), start, days):.2f}%" '
                      f'title="today {today.isoformat()}"></span>')
    rows = []
    for p in projects:
        bars = []
        for lane, m in enumerate(p["milestones"]):
            left = _planner_axis_pos(m["start"], start, days)
            right = _planner_axis_pos(m["target"], start, days)
            # The bar spans start -> target and carries the colour; the
            # diamond marks the target date; the label sits in the lane,
            # never clipped by a short bar, and flips to the left of the
            # target when the target is in the right third of the axis.
            label_side = (f"right:{100 - right:.2f}%;padding-right:.9rem;text-align:right"
                          if right > 66 else f"left:{right:.2f}%;padding-left:.8rem")
            title = (f'{esc(m["title"])} · target {esc(m["target"])} · '
                     f'{esc(m["state"])}: {esc(m["reason"])}')
            top = lane * 1.9
            bars.append(
                f'<span class="plan-bar schedule-{esc(m["state"])}" '
                f'style="left:{left:.2f}%;width:{max(right - left, 0.6):.2f}%;'
                f'top:{top:.1f}rem" title="{title}"></span>'
                f'<span class="plan-diamond schedule-{esc(m["state"])}" '
                f'style="left:{right:.2f}%;top:{top:.1f}rem" title="{title}"></span>'
                f'<a class="plan-label" href="{esc(p["url"])}#m-{esc(m["id"])}" '
                f'style="{label_side};top:{top:.1f}rem" title="{title}">'
                f'<strong>{esc(m["state"])}</strong> · {esc(m["title"])} · '
                f'{esc(m["target"][5:])}</a>')
        height = max(1, len(p["milestones"])) * 1.9 + 0.3
        rows.append(
            f'<div class="plan-row"><div class="plan-name">'
            f'<a href="{esc(p["url"])}">{esc(p["title"])}</a></div>'
            f'<div class="plan-track" style="height:{height:.1f}rem">'
            f'{today_line}{"".join(bars)}</div></div>')
    return (f'<div class="plan-scroll"><div class="plan-grid">'
            f'<div class="plan-row plan-axis"><div class="plan-name"></div>'
            f'<div class="plan-track">{"".join(ticks)}</div></div>'
            f'{"".join(rows)}</div></div>')


def _planner_milestone_rows(project: dict, with_items: bool) -> str:
    out = []
    for m in project["milestones"]:
        tracker = (f' · <a href="{esc(m["tracker"])}">tracker</a>'
                   if m.get("tracker") else "")
        out.append(
            f'<tr id="m-{esc(m["id"])}" class="schedule-{esc(m["state"])}">'
            f'<td>{_schedule_word(m["state"])}</td>'
            f'<td>{esc(m["title"])}{tracker}</td>'
            f'<td>{esc(m["target"])}</td>'
            f'<td>{m["closed"]} / {m["of"]}</td>'
            f'<td>{esc(m["reason"])}</td></tr>')
        if with_items and m["items"]:
            items = "".join(
                "<li>"
                + (f'<a href="{esc(i["url"])}">{esc(i["ref"])}</a>' if i.get("url")
                   else esc(i["ref"]))
                + (f' · {esc(i["title"])}' if i.get("title") else "")
                + (" · closed" if i["closed"] else
                   (" · <strong>UNREADABLE</strong>" if not i["readable"] else " · open"))
                + (f' · <strong class="plan-blocker">{esc(i["blocker"])}</strong>'
                   if i.get("blocker") else "")
                + "</li>"
                for i in m["items"])
            out.append(f'<tr class="plan-items"><td></td><td colspan="4">'
                       f'<ul>{items}</ul></td></tr>')
    head = ("<tr><th>state</th><th>milestone</th><th>target</th>"
            "<th>items closed</th><th>why</th></tr>")
    return f'<table class="plan-table">{head}{"".join(out)}</table>'


def _planner_counts(project: dict) -> str:
    return " · ".join(f'{n} {_schedule_word(s)}'
                      for s, n in project["states"].items() if n)


def planner_page(index: Index) -> str:
    """The delivery calendar (alpha-engine-config-I12152): every declared
    project's milestones side by side on one dated axis, then each project's
    milestones with the reason for its state. Declared in config, evaluated
    per request, nothing stored (§5.6)."""
    from ..index import planner

    projects = planner.evaluate(index)
    if projects:
        sections = "".join(
            f'<h2><a href="{esc(p["url"])}">{esc(p["title"])}</a></h2>'
            f'<p>{_planner_counts(p)}'
            + (f' · owner {esc(p["owner"])}' if p.get("owner") else "")
            + "</p>"
            + _planner_milestone_rows(p, with_items=False)
            for p in projects)
    else:
        sections = '<p class="absent">no projects declared — the `planner:` config block is empty</p>'
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>planner</title></head><body>
<nav><a href="/">fleet</a> &rsaquo; planner</nav>
<h1>planner</h1>
<p class="pane-question">{esc(planner.PLANNER_QUESTION)}</p>
{index_freshness(index)}
{_planner_timeline(projects)}
{sections}
</body></html>"""


def planner_project_page(index: Index, project_id: str) -> str:
    """One project: each milestone, its reason, and every required item with
    its blockers named, open and blocked first."""
    from ..index import planner

    p = planner.project(index, project_id)
    if p is None:
        return f"""<!doctype html><html><head><meta charset="utf-8">
<title>planner</title></head><body>
<p class="absent">no planner project {esc(project_id)}</p></body></html>"""
    tracker = (f' · <a href="{esc(p["tracker"])}">tracker</a>'
               if p.get("tracker") else "")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(p["title"])} · planner</title></head><body>
<nav><a href="/">fleet</a> &rsaquo; <a href="/planner">planner</a> &rsaquo; {esc(p["title"])}</nav>
<h1>{esc(p["title"])}</h1>
<p class="pane-question">{esc(planner.PLANNER_QUESTION)}</p>
<p>{_planner_counts(p)}{(' · owner ' + esc(p["owner"])) if p.get("owner") else ""}{tracker}</p>
{index_freshness(index)}
{_planner_timeline([p])}
{_planner_milestone_rows(p, with_items=True)}
</body></html>"""


def with_site_nav(page: str) -> str:
    """Prepend the one site menu to a rendered page: home · dashboards ·
    search. Applied once, at the server, so no page can ship without it."""
    nav = ('<nav class="site-nav"><a href="/">home</a> · '
           '<a href="/dashboards">dashboards</a> · '
           '<a href="/planner">planner</a> · '
           '<a href="/search">search</a></nav>')
    return page.replace("<body>", "<body>" + nav, 1)


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


def milestones_section(declared: list[dict], recorded_journal: list[dict]) -> str:
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

    Takes the already-evaluated predicate and the already-read journal rather
    than an `Index` — both come from `index.landing_model()` now
    (alpha-engine-config-I10615), so this function only formats; it never
    re-runs `milestone_predicates.evaluate`/`journal_report` a second time in
    the same request.
    """
    if not declared:
        return ""
    recorded = {r.get("milestone_id"): r for r in recorded_journal}
    return "".join(_milestone(m, recorded.get(m["id"])) for m in declared)


def _milestone(m: dict, recorded: dict | None = None) -> str:
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
        f'<h2 id="milestone-{esc(m["id"])}">milestone: {esc(m["id"])}</h2>'
        # §4.4: the question sentence is rendered on the pane, not left in a
        # registry a reader never opens.
        f'<p class="pane-question">{esc(_MILESTONE_PANE_QUESTION)} &mdash; '
        f'{esc(m["question"])}</p>'
        f'<p>{m["met"]} of {m["of"]} clauses met{unreported}{holding}{tracker}</p>'
        f'{_milestone_predicate(m)}'
        "<table><thead><tr><th>clause</th><th>status</th><th>bound to</th>"
        "<th>value</th><th>target</th><th>as-of</th><th>evidence</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f'{_milestone_history(recorded)}'
    )


def _milestone_predicate(m: dict) -> str:
    """The whole predicate as the one field a machine reads, rendered so the
    predicate a sweep evaluates is COPIED OFF THE SURFACE rather than
    transcribed by hand — a transcribed predicate is the one that ends up
    malformed and silently never evaluates (gate-taxonomy-policy.md §5)."""
    state = m.get("exit_state")
    predicate = (
        f' &middot; <code>{esc(m["verified_when"])}</code>'
        if m.get("verified_when") else
        ' &middot; <em class="absent">no journal declared — this predicate is '
        'not machine-checkable and no clause transition is recorded</em>'
    )
    return (f'<p>exit state: <strong>{esc(str(state))}</strong>'
            f'{predicate}</p>')


def _milestone_history(recorded: dict | None) -> str:
    """What the durable clause journal recorded, rendered back.

    A transition is a FACT THE CONSOLE HOLDS, not something a reader
    reconstructs by remembering last week's number — which is precisely how two
    clauses went MET -> UNMET in three days with nobody noticing
    (alpha-engine-config-I9083). A journal that could not be read or written
    renders LOUDLY here: a recorder failing silently is the defect this exists
    to remove, so its own failure may not be silent either.
    """
    if not recorded:
        return ""
    if recorded.get("error"):
        return ('<p class="state-UNREPORTED">clause journal UNREADABLE: '
                f'{esc(str(recorded["error"]))} &mdash; no transition is being '
                'recorded, so a clause could regress unannounced</p>')
    rows = "".join(
        f'<tr class="milestone-{esc(str(t.get("to")))}">'
        f'<td>{esc(str(t.get("at")))}</td>'
        f'<td>{esc(str(t.get("clause")))}</td>'
        f'<td>{esc(str(t.get("from")))} &rarr; {esc(str(t.get("to")))}'
        f'{" (baseline)" if t.get("baseline") else ""}</td>'
        f'<td>{esc(_moved_text(t))}</td></tr>'
        for t in reversed(recorded.get("recent") or [])
    )
    pending = recorded.get("undelivered_notifications")
    warn = (f'<p class="state-UNREPORTED">{pending} transition notification(s) '
            "undelivered — recorded, not announced</p>") if pending else ""
    if not rows:
        return (f'<p>clause journal: <code>{esc(str(recorded.get("journal")))}'
                "</code> &middot; no transition recorded yet</p>" + warn)
    return (
        f'<p>clause journal: <code>{esc(str(recorded.get("journal")))}</code>'
        f' &middot; retained {esc(str(recorded.get("retention_hours")))}h</p>'
        + warn
        + "<table><thead><tr><th>at</th><th>clause</th><th>transition</th>"
        "<th>what moved</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _moved_text(transition: dict) -> str:
    moved = transition.get("moved") or []
    if not moved:
        return "—"
    return "; ".join(
        f'{m.get("binding")} {m.get("from")} → {m.get("to")}'
        for m in moved
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
#: Above this many members, the inline enumeration stops being navigation and
#: starts being the page. The filtered listing link is complete either way, so
#: nothing is hidden — only the prose is bounded.
_INLINE_MEMBER_LIMIT = 25


def observation_coverage_line(index: Index) -> str:
    """Declared artifacts anything actually LOOKED at (alpha-engine-config-I8765).

    Not one of §9's nine. It exists because `unobserved` — the honest state of a
    declared row with no observation half — is deliberately not an exception,
    and a state that is not an exception is invisible on this view. Without this
    line, removing 177 false `absent` findings would look exactly like the fleet
    having got better, which is the failure §5.5 names: no data rendered as
    green. The members are linked, so the coverage gap is navigable and not
    merely counted (§3.1, §5.1).
    """
    cov = artifact_observation_coverage(index)
    if cov.get("computable") is False:
        return (f'<p>artifact observation coverage not computable — '
                f'{esc(cov.get("reason", "no reason given"))}</p>')
    members = list(cov.get("unobserved_ids") or ())
    if len(members) > _INLINE_MEMBER_LIMIT:
        listing = path_for_list(Kind.ARTIFACT, {STATE_FILTER: UNOBSERVED_VALUE})
        links = (f' — <a href="{esc(listing)}">all {esc(UNOBSERVED_VALUE)} '
                 f'{esc(Kind.ARTIFACT.value)}s</a> ({len(members)} rows)')
    else:
        links = _member_links(Kind.ARTIFACT, UNOBSERVED_VALUE, members)
    return (f'<p>artifact observation coverage {cov["count"]} / {cov["of"]} '
            f'declared artifacts observed · {len(members)} declared and never '
            f'looked at{links}</p>')


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
