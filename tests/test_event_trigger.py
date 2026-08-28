"""§8.3 ARMED — the healthy-when-silent state for an event-driven component
(`alpha-engine-config-I7116`).

Mirrors `test_cadence_state.py`'s shape: every case builds the claims an
adapter would actually produce and asserts on the MERGED entity as
`Index.finalize()` produces it, so the test exercises the wiring
(`graph.py::finalize` -> `event_trigger.resolve_event_trigger_state`) and not
just the bare function.

The live case this guards: `alpha-engine-arctic-migration-dispatcher`
(`cadence: event_driven`, `event_trigger_anchor` naming the GitHub Actions
workflow component that invokes it) must leave `UNREPORTED` once its anchor
is declared and resolves HEALTHY. The case it must never allow: a fabricated
event-driven component whose declared anchor does not exist, or resolves
broken, must stay in the transparency gap — "a state that can never be
non-healthy is worse than no state at all" (`champion-challenger-policy.md`
§7.4).
"""
from __future__ import annotations

from datetime import datetime, timezone

from console.index.event_trigger import resolve_event_trigger_state
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _registry_row(component_id: str, *, anchor_id: str | None, cadence: str = "event_driven") -> AdapterResult:
    detail = {"registry_file": f"{component_id}.yaml", "produces": [], "consumes": [], "cadence": cadence}
    if anchor_id is not None:
        detail["event_trigger_anchor"] = anchor_id
    return AdapterResult(
        claim_class=ClaimClass.DECLARATION,
        fetched_at=_now_iso(),
        name="registry",
        status=AdapterStatus.OK,
        entities=(Entity(
            kind=Kind.COMPONENT,
            id=component_id,
            state=State.UNREPORTED,
            provenance=Provenance(source="registry.d", as_of=None, evidence="file://x"),
            facets={"substrate": "lambda"},
            detail=detail,
        ),),
    )


def _anchor_declaration(anchor_id: str) -> AdapterResult:
    """The anchor's own registry row (`alpha-engine-config-I6835`: workflows
    are registered in `governance/observability.d/` like any other
    component). Carries no lifecycle, so its own default is `UNREPORTED`
    (rank 3) and the workflow adapter's DISCOVERY claim (rank 2) wins the
    merged state — exactly the shape a live `github-actions` component has."""
    return AdapterResult(
        claim_class=ClaimClass.DECLARATION,
        fetched_at=_now_iso(),
        name="registry",
        status=AdapterStatus.OK,
        entities=(Entity(
            kind=Kind.COMPONENT,
            id=anchor_id,
            state=State.UNREPORTED,
            provenance=Provenance(source="registry.d", as_of=None, evidence="file://x"),
        ),),
    )


def _anchor_workflow(anchor_id: str, state: State) -> tuple[AdapterResult, AdapterResult]:
    """The GitHub Actions workflow Component `adapters/git_host.py` would
    emit — a DISCOVERY claim, exactly as that adapter's own claim class is —
    plus its own registry declaration, since a live `github-actions`
    component carries both (`alpha-engine-config-I6835`).

    Scoped to its own repo (`discovery_scope`) — the real adapter enumerates
    ONE repo's workflows, so a fixture that leaves this fleetwide would
    falsely flip every OTHER declared-but-unobserved Component in the same
    build to `ABSENT` (`graph.py::_within_discovery_scope`), which is not
    this adapter's job to prove and not what §8.3's ABSENT guard is for.
    """
    discovery = AdapterResult(
        claim_class=ClaimClass.DISCOVERY,
        fetched_at=_now_iso(),
        name="repos",
        status=AdapterStatus.OK,
        discovery_scope=(("repo", "nousergon-data"),),
        entities=(Entity(
            kind=Kind.COMPONENT,
            id=anchor_id,
            state=state,
            provenance=Provenance(source="git-host:repo:wf.yml", as_of=_now_iso(), evidence="https://x"),
            facets={"repo": "nousergon-data"},
            detail={"workflow": "run-arctic-migrations"},
        ),),
    )
    return _anchor_declaration(anchor_id), discovery


def _build(*results: AdapterResult) -> Index:
    idx = Index()
    for r in results:
        idx.add_result(r)
    return idx.finalize()


# --------------------------------------------------------------- the live case

def test_healthy_anchor_arms_the_silent_event_driven_component():
    idx = _build(
        _registry_row("alpha-engine-arctic-migration-dispatcher", anchor_id="nousergon-data--run-arctic-migrations"),
        *_anchor_workflow("nousergon-data--run-arctic-migrations", State.HEALTHY),
    )
    ent = idx.entity("alpha-engine-arctic-migration-dispatcher")
    assert ent.state is State.ARMED


def test_never_ran_anchor_still_arms():
    """The trigger has literally never fired — the wiring is intact and
    unproven, not broken. NEVER_RAN is in the intact set."""
    idx = _build(
        _registry_row("dispatcher-b", anchor_id="anchor-b"),
        *_anchor_workflow("anchor-b", State.NEVER_RAN),
    )
    assert idx.entity("dispatcher-b").state is State.ARMED


def test_armed_component_clears_the_transparency_gap():
    idx = _build(
        _registry_row("alpha-engine-arctic-migration-dispatcher", anchor_id="anchor-ok"),
        *_anchor_workflow("anchor-ok", State.HEALTHY),
    )
    gap = idx.transparency_gap()
    assert gap["count"] == 0
    assert gap["of"] == 2  # the dispatcher AND its anchor are both Components


# ------------------------------------------------- the fabricated broken case

def test_missing_anchor_declaration_stays_unreported():
    """cadence: event_driven with NO anchor declared — today's live shape for
    every row not yet updated (`alpha-engine-arctic-migration-dispatcher`'s
    row before a companion registry PR adds the anchor field). Must not be
    granted ARMED from the bare declaration."""
    idx = _build(_registry_row("dispatcher-c", anchor_id=None))
    assert idx.entity("dispatcher-c").state is State.UNREPORTED


def test_unresolvable_anchor_stays_unreported():
    """The fabricated case: the declared anchor id names nothing in this
    build — a deleted workflow, a typo, a trigger genuinely removed."""
    idx = _build(_registry_row("dispatcher-fabricated", anchor_id="does-not-exist"))
    ent = idx.entity("dispatcher-fabricated")
    assert ent.state is State.UNREPORTED


def test_failed_anchor_withholds_armed():
    """The anchor resolves, but its own last run failed — the wiring is
    provably NOT intact. Must not mask that as healthy silence."""
    idx = _build(
        _registry_row("dispatcher-d", anchor_id="anchor-d"),
        *_anchor_workflow("anchor-d", State.FAILED),
    )
    assert idx.entity("dispatcher-d").state is State.UNREPORTED


def test_disabled_anchor_withholds_armed():
    """The workflow itself was disabled at the host — the trigger is gone in
    the way that matters, even though the row still exists."""
    idx = _build(
        _registry_row("dispatcher-e", anchor_id="anchor-e"),
        *_anchor_workflow("anchor-e", State.DISABLED),
    )
    assert idx.entity("dispatcher-e").state is State.UNREPORTED


def test_absent_anchor_withholds_armed():
    idx = _build(
        _registry_row("dispatcher-f", anchor_id="anchor-f"),
        *_anchor_workflow("anchor-f", State.ABSENT),
    )
    assert idx.entity("dispatcher-f").state is State.UNREPORTED


# --------------------------------------------------------- precedence guards

def test_a_real_failed_reading_is_never_overridden_to_armed():
    """§8.3: this resolver only ever moves a row OFF UNREPORTED. A component
    that genuinely reported FAILED must keep that reading even if its row
    also declares cadence: event_driven and a healthy anchor."""
    failed_claim = AdapterResult(
        claim_class=ClaimClass.OBSERVATION,
        fetched_at=_now_iso(),
        name="cloudwatch",
        status=AdapterStatus.OK,
        entities=(Entity(
            kind=Kind.COMPONENT,
            id="dispatcher-g",
            state=State.FAILED,
            provenance=Provenance(source="cloudwatch", as_of=_now_iso(), evidence="https://x"),
        ),),
    )
    idx = _build(
        _registry_row("dispatcher-g", anchor_id="anchor-g"),
        *_anchor_workflow("anchor-g", State.HEALTHY),
        failed_claim,
    )
    assert idx.entity("dispatcher-g").state is State.FAILED


def test_non_event_driven_cadence_is_untouched():
    idx = _build(_registry_row("dispatcher-h", anchor_id="anchor-h", cadence="weekday_sf"))
    assert idx.entity("dispatcher-h").state is State.UNREPORTED


# --------------------------------------------------------- unit-level (bare function)

def test_resolver_ignores_non_component_run_kinds():
    art = Entity(
        kind=Kind.ARTIFACT,
        id="some/key",
        state="stale",
        provenance=Provenance(source="s3"),
        detail={"cadence": "event_driven", "event_trigger_anchor": "anchor-i"},
    )
    out = resolve_event_trigger_state({"some/key": art})
    assert out["some/key"] is art


def test_resolver_preserves_the_full_key_set():
    """Never adds or removes an identifier — only ever replaces a value."""
    ent = Entity(
        kind=Kind.COMPONENT, id="x", state=State.UNREPORTED,
        provenance=Provenance(source="registry.d"),
        detail={"cadence": "event_driven"},
    )
    out = resolve_event_trigger_state({"x": ent})
    assert set(out) == {"x"}
