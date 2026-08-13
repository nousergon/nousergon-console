"""DISABLED vs MISSED, decided where both claims are visible (§2.5).

`observability-policy.md` §8.3 keeps `MISSED` (a defect — the schedule fired or
should have, and nothing ran) apart from `DISABLED` (a decision) and from
`HEALTHY`. A substrate-counter adapter cannot make that call: zero invocations
in a trailing window is idle-by-design and missed-its-trigger wearing the same
face, and `console-policy.md` §2.3 forbids the adapter from reading the
registry that carries the expectation. So `adapters/cloudwatch_metrics.py`
renders a silent component `UNREPORTED` with its reason attached, and leaves
it counted in the transparency gap.

**The comparison belongs here, in the merge**, because here — and only here —
the registry's declared `cadence_minutes` and the substrate's observed last
invocation are both present on one entity (§2.5's declaration/observation
merge). Putting it in the adapter would require the adapter to read a second
source; putting it in a renderer would make one view disagree with another.

## The rule

For a merged Component that a discovery/observation claim reported SILENT — no
invocations in its window — and whose merged detail carries a positive
`cadence_minutes`:

- silence longer than ``cadence_minutes * staleness_factor``  → `MISSED`
- silence within it                                           → `HEALTHY`

## Why the second line is not "no data rendered green"

It is not "no data". A silent component reaching this function carries a
`last_invocation` read from the substrate's own history: the component DID run,
inside the period its registry row says it is expected to run in, and the only
thing the 1440-minute window established is that the run was older than the
window — which for a weekly component is the normal case and says nothing.
Widening the adapter's window to reach the same conclusion is the failure this
avoids: the window would then paint every silent component green regardless of
what any of them was supposed to do.

The guard that keeps it honest is `_silence_minutes` returning ``None``
whenever the substrate could not say when the component last ran. Absent a last
invocation there is no evidence of anything, and the entity is returned
untouched — still `UNREPORTED`, still a finding, still counted.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from ..model.entity import Entity
from ..model.kinds import COMPONENT_STATE_KINDS, State
from .numbers import staleness_threshold_seconds

#: Detail keys a silent substrate-counter claim leaves behind. `invocations`
#: is the discriminator (zero, over a window the adapter also names); the rest
#: of the row's detail is not read here.
_INVOCATIONS = "invocations"
_WINDOW = "window_minutes"
_LAST = "last_invocation"
_CADENCE = "cadence_minutes"


def resolve_cadence_state(
    ent: Entity,
    now: datetime | None = None,
    staleness_factor: float = 1.5,
) -> Entity:
    """Place a silent component against its own declared cadence.

    Returns the entity unchanged whenever any input is missing — a component
    with no declared cadence, no observed silence, or no last invocation is
    exactly the one this cannot place, and `UNREPORTED` is §8.3's answer for
    that. Never manufactures a cadence, and never widens a window.
    """
    if ent.kind not in COMPONENT_STATE_KINDS:
        return ent
    if ent.state is not State.UNREPORTED:
        # A declared lifecycle (DISABLED/DEPRECATED/RETIRED) has already won the
        # merge and is absolute (§8.3); FAILED/HEALTHY came from real counters.
        # This function only ever moves a row OFF `UNREPORTED`.
        return ent
    if not _is_observed_silent(ent.detail):
        return ent
    cadence = _positive(ent.detail.get(_CADENCE))
    if cadence is None:
        return ent
    silence = _silence_minutes(ent.detail.get(_LAST), now)
    if silence is None:
        return ent
    threshold = staleness_threshold_seconds(
        cadence * 60.0, _observer_cadence_seconds(ent), staleness_factor
    ) / 60.0
    if silence > threshold:
        return dataclasses.replace(ent, state=State.MISSED)
    return dataclasses.replace(ent, state=State.HEALTHY)


def _observer_cadence_seconds(ent: Entity) -> float:
    """How long the counter-reading source may itself have lagged.

    The second instance of the class alpha-engine-config-I7126 fixed in §9.6:
    `last_invocation` is not "when it last ran", it is "when the source that
    polls every N seconds last SAW it run". Comparing that against a declared
    cadence with no term for N decides the verdict on the phase offset between
    two schedules. Shares one helper with `numbers.py` so the two cannot drift
    — the same reason `Index._staleness_factor` is one value, not two.

    Where the source declares no cadence this returns 0, which is the previous
    behaviour exactly. §9.6 EXCLUDES such a row instead, and the asymmetry is
    deliberate: §9.6 is an audit, where declining to grade an unbounded
    reading is the honest answer, while this function ASSIGNS a state, where
    declining means falling back to `UNREPORTED` — discarding a real
    substrate reading and inflating the transparency gap. Widening on a
    declared number and not widening on an absent one is strictly the safer
    half of that trade in both directions.
    """
    provenance = ent.source_of(f"detail.{_LAST}")
    cadence = provenance.cadence_seconds
    if cadence is None:
        cadence = ent.provenance.cadence_seconds
    return float(cadence) if cadence else 0.0


def _is_observed_silent(detail: dict) -> bool:
    """Did a source actually READ a window and find zero, or is this a bare row?

    A registry-only row has neither key. Requiring both is what stops a
    declaration's cadence being compared against nothing at all — the entity
    would then flip to `HEALTHY` on a file, which is the exact inversion §2.5's
    state precedence exists to prevent.
    """
    if _INVOCATIONS not in detail or _WINDOW not in detail:
        return False
    try:
        return float(detail[_INVOCATIONS]) == 0.0
    except (TypeError, ValueError):
        return False


def _positive(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _silence_minutes(last_invocation, now: datetime | None) -> float | None:
    if not last_invocation:
        return None
    try:
        stamp = datetime.fromisoformat(str(last_invocation).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - stamp).total_seconds() / 60.0)
