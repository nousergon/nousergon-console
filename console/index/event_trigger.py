"""ARMED — the healthy-when-silent state for an event-driven component
(`observability-policy.md` §8.3, `alpha-engine-config-I7116`).

A component whose trigger is a genuine event rather than a clock — an
EventBridge event-pattern rule, a merge-triggered CI workflow, a queue
consumer — has no `cadence_minutes` an honest classifier can derive:
declaring one manufactures an expectation the component never made (the exact
`MISSED` false-positive `nous-ergon-ops-PR624` hit). `calendar_cadence.py`
already refuses to translate `event_driven` into a minute ceiling for exactly
this reason, so `index/cadence_state.py` — which only ever moves a row OFF
`UNREPORTED` by comparing an observed silence against a positive
`cadence_minutes` — has nothing to work with, and the row stays `UNREPORTED`
forever: correct, and uncleared by construction.

**This is a RELATION, not a bare declaration.** Unlike `DISABLED` /
`DEPRECATED` / `RETIRED` (trusted from the registry row's own say-so, per
§8.3: "declared in the registry, never inferred"), `ARMED` may not be granted
from `cadence: event_driven` alone — a disposition an operator's own row can
assert unconditionally is a guard that can never fail, and
`champion-challenger-policy.md` §7.4 is explicit that such a guard is
indistinguishable from no guard at all. So a row declaring itself
event-driven also declares a **trigger anchor**: the identifier of a
separately-observed entity whose own state stands in for "the mechanism that
would fire this component still exists" — the fleet's concrete case is
`console/adapters/git_host.py`'s own Component row for the GitHub Actions
workflow that invokes the Lambda, itself already rendered from the workflow's
real run history (`NEVER_RAN` / `DISABLED` / mapped-from-conclusion).

`ARMED` is granted only when the anchor **resolves** to a known entity in the
same build **and** that entity's own state is one that says the trigger is
intact. Two ways this refuses, deliberately, rather than granting a green
that cannot be trusted:

- **No anchor declared, or the anchor id does not resolve** (wrong id, the
  anchor's own adapter is disabled or unreachable this build, the anchor was
  renamed and the row was not updated) — the row this exists for:
  a fabricated or genuinely broken event-driven component. Stays
  `UNREPORTED`, still counted, still a finding — exactly what `UNREPORTED`
  is for.
- **The anchor resolves but is itself broken** (`FAILED`, `DISABLED` at the
  host, `ABSENT`, `UNREGISTERED`) — the wiring is provably not intact. Stays
  `UNREPORTED` rather than silently propagating a different state; the
  anchor's own row is where that finding belongs.

Runs after the main merge/reconcile pass, in `graph.py::finalize`, because it
needs the FULL resolved entity set to look the anchor up — `_reconcile` runs
per-identifier during the claim-merge loop, before every entity is known.
"""
from __future__ import annotations

import dataclasses

from ..model.entity import Entity
from ..model.kinds import COMPONENT_STATE_KINDS, State

#: The registry-declared calendar symbol for "no schedule, by declaration"
#: (`calendar_cadence.EVENT_DRIVEN`) — duplicated as a literal here rather
#: than imported to avoid a dependency from the index layer onto the cadence
#: translator for one string; kept identical by `tests/test_event_trigger.py`.
EVENT_DRIVEN_CADENCE = "event_driven"

#: An anchor's own state must say "the trigger mechanism is intact" before its
#: silence lends ARMED to anything. `HEALTHY` — the anchor's last concluded
#: run (its own workflow/rule/queue-consumer) ended ok. `NEVER_RAN` — the
#: anchor exists, is enabled, and has simply never fired yet; a component
#: whose trigger has literally never occurred is still armed, not broken.
#: `RUNNING` — the anchor is mid-execution, which is itself evidence the
#: wiring fires. Deliberately narrow: `DEGRADED`, `STALLED`, `MISSED`,
#: `FAILED`, `DISABLED`, `DEPRECATED`, `RETIRED`, `ABSENT`, `UNREGISTERED` and
#: `UNREPORTED` all withhold ARMED, because each says the mechanism is either
#: broken, off, or itself unproven.
_ANCHOR_INTACT_STATES: frozenset[State] = frozenset(
    {State.HEALTHY, State.NEVER_RAN, State.RUNNING}
)

_CADENCE_KEY = "cadence"
_ANCHOR_KEY = "event_trigger_anchor"


def resolve_event_trigger_state(entities: dict[str, Entity]) -> dict[str, Entity]:
    """Move a declared-event-driven, still-`UNREPORTED` Component to `ARMED`
    wherever its declared trigger anchor resolves to a verifiably intact
    entity in this same build. Every other row is returned unchanged.

    Never mutates in place — `Entity` is frozen — and never removes an
    identifier: the return value has exactly the same key set as the input.
    """
    out = dict(entities)
    for entity_id, ent in entities.items():
        armed = _armed_replacement(ent, entities)
        if armed is not None:
            out[entity_id] = armed
    return out


def _armed_replacement(ent: Entity, entities: dict[str, Entity]) -> Entity | None:
    if ent.kind not in COMPONENT_STATE_KINDS:
        return None
    if ent.state is not State.UNREPORTED:
        # Only ever moves a row OFF UNREPORTED (§8.3) — a declared lifecycle,
        # a real FAILED/HEALTHY reading, or an already-resolved MISSED/HEALTHY
        # from `cadence_state.py` all outrank a bare event-driven declaration.
        return None
    if str(ent.detail.get(_CADENCE_KEY) or "").strip().lower() != EVENT_DRIVEN_CADENCE:
        return None
    anchor_id = ent.detail.get(_ANCHOR_KEY)
    if not anchor_id or not str(anchor_id).strip():
        # Declared event-driven with no anchor to verify against — honestly
        # unplaceable, exactly as before this state existed.
        return None
    anchor = entities.get(str(anchor_id).strip())
    if anchor is None:
        # The anchor does not resolve in this build: wrong id, its own
        # adapter did not run, or it was renamed out from under this row.
        # This is the "trigger no longer exists" case ARMED must never mask.
        return None
    if not isinstance(anchor.state, State) or anchor.state not in _ANCHOR_INTACT_STATES:
        # Resolves, but the mechanism itself is broken, off, or unproven.
        return None
    return dataclasses.replace(ent, state=State.ARMED)
