"""§9.5 orphan counts and §9.6 staleness honesty.

The two remaining §9 numbers that are pure graph/registry properties, as
distinct from execution (§9.3, in `reachability.py`), history (§9.8's git
archaeology, in `onboarding.py`) or a standing question set (§9.4, in
`console/qa/questions.py`). Kept together because both are cheap, pure
functions of an already-built `Index` and are recomputed on every query
rather than cached (matching `reachability()` and `conflicts()`, not the
build-time-cached `onboarding_cost`/`answer_latency`).
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..model.kinds import State
from ..render.panes import orphan_counts as _pane_orphan_counts

#: Rendered values that ALREADY say "this is stale" — a row carrying one of
#: these has disclosed its own age (§5.2 doing its job), so it is not a
#: staleness-HONESTY finding even though it is, in fact, stale.
_DISCLOSED_COMPONENT_STATES = frozenset(
    {State.MISSED, State.STALLED, State.UNREPORTED, State.ABSENT}
)
_DISCLOSED_VALUES = frozenset(
    {"stale", "no-freshness-stamp", "no-cadence-declared", "unreadable"}
)


def orphan_counts(index=None) -> dict[str, object]:
    """§9.5 — delegates to the pane registry.

    ``index`` is accepted and unused so every §9 number on `Index` shares one
    calling convention (`index.<number>()`); the orphan check is a property of
    the pane registry and the closed `Kind` set, not of any particular graph.
    """
    return _pane_orphan_counts()


def staleness_honesty(
    index, now: datetime | None = None, staleness_factor: float = 1.5
) -> dict[str, object]:
    """§9.6 — rows older than their OWN declared cadence with no staleness
    marker. Target 0.

    Independently RE-DERIVES staleness from each entity's own declared cadence
    (currently `detail["cadence_minutes"]`, the one per-entity cadence the
    fleet's envelope carries — `checks_envelope.py`) and its `as_of`, rather
    than trusting the state an adapter already assigned. An honesty check that
    only re-reads the thing it is checking would never find the bug it exists
    for: an adapter whose staleness branch stopped firing while its `status`
    field kept reporting `ok`.

    Only entities carrying BOTH a cadence and an as-of are auditable; the rest
    are excluded from the denominator rather than counted as a pass (§5.3) —
    a row with nothing to check it against is unaudited, not honest.
    """
    now = now or datetime.now(timezone.utc)
    checked = 0
    violations: list[str] = []
    for ent in index.all():
        cadence_minutes = ent.detail.get("cadence_minutes")
        as_of = ent.provenance.as_of
        if not cadence_minutes or not as_of:
            continue
        try:
            cadence_seconds = float(cadence_minutes) * 60.0
            ts = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        checked += 1
        age = (now - ts).total_seconds()
        if age <= cadence_seconds * staleness_factor:
            continue  # genuinely fresh
        disclosed = (
            isinstance(ent.state, State) and ent.state in _DISCLOSED_COMPONENT_STATES
        ) or (
            isinstance(ent.state, str) and ent.state.strip().lower() in _DISCLOSED_VALUES
        )
        if not disclosed:
            violations.append(ent.id)
    return {"count": len(violations), "of": checked, "violations": sorted(violations)}
