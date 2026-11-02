"""OFF_DUTY — a component whose substrate was proven powered down
(`observability-policy.md` §8.3, `alpha-engine-config-I12940`).

The laptop is a substrate with no fixed duty window: it sleeps when the lid
closes. Every laptop-launchd producer declares a flat wall-clock cadence, so a
night of sleep rendered ~40 rows `MISSED` / `stale` every morning — true in the
narrow sense that nothing ran, but not defects, and they buried the real
overnight exceptions. `MISSED` means "the schedule fired or should have, and no
run started"; a launchd agent on a sleeping machine has no schedule firing.

**This is a RELATION, not a bare declaration** — the same shape as `ARMED`
(`index/event_trigger.py`). A row may declare `duty_anchor: <component_id>`:
the separately-observed component whose own heartbeat stands in for "this
substrate is powered and on duty" (the fleet's case:
`ai-nousergon-observability-heartbeat`, the laptop's own launchd heartbeat
publisher). Nothing a row says about itself can make it `OFF_DUTY`; only the
anchor's own observed silence can.

A `MISSED` row moves to `OFF_DUTY` only when ALL of these hold:

1. it declares a `duty_anchor` that is not its own id;
2. the anchor resolves in this build and is itself `MISSED` — the substrate's
   proof of life is overdue. Any other anchor state (`HEALTHY`, `FAILED`,
   `UNREPORTED`, absent from the build, …) proves nothing about the substrate
   being down, so the row stays `MISSED`;
3. the row was **within its own window at the anchor's last proof of life**:
   ``row_last_run + row_cadence * staleness_factor >= anchor_last_run``. A job
   that had already missed while the substrate was demonstrably up stays
   `MISSED` — going to sleep afterwards does not launder it;
4. every timestamp and cadence involved parses. An input this cannot read
   leaves the row `MISSED`: the fold only ever moves a row on evidence.

The anchor itself never folds — it is the one row that keeps the substrate
visible, so a laptop that never wakes is still a finding. When the anchor is
current again, every child grades on its own cadence exactly as before, so a
dead agent on an awake laptop is `MISSED` within one heartbeat.

The `…/latest.json` artifact a folded component's envelope lives in
(`adapters/checks_envelope.py`, `detail.check_id`) carries the raw value
`off-duty` instead of `stale` for the same reason and only in the same case.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from ..model.entity import Entity
from ..model.kinds import Kind, State

#: The registry field naming the substrate's liveness anchor
#: (`adapters/yaml_directory.py` carries it into `detail`).
DUTY_ANCHOR_KEY = "duty_anchor"
#: The raw value an off-duty component's envelope artifact carries instead of
#: `stale`. Not in `EXCEPTION_VALUES`; disclosed to §9.6.
OFF_DUTY_VALUE = "off-duty"

_CADENCE = "cadence_minutes"


def resolve_duty_window_state(
    entities: dict[str, Entity],
    staleness_factor: float = 1.5,
) -> dict[str, Entity]:
    """Move every eligible `MISSED` component to `OFF_DUTY`, then its envelope
    artifact from `stale` to `off-duty`. Same key set in as out; never mutates.
    """
    out = dict(entities)
    folded: set[str] = set()
    for entity_id, ent in entities.items():
        replacement = _off_duty_replacement(ent, entities, staleness_factor)
        if replacement is not None:
            out[entity_id] = replacement
            folded.add(entity_id)
    if not folded:
        return out
    for entity_id, ent in entities.items():
        if ent.kind is not Kind.ARTIFACT:
            continue
        if str(ent.detail.get("check_id") or "") not in folded:
            continue
        if str(ent.state).strip().lower() != "stale":
            continue
        out[entity_id] = dataclasses.replace(ent, state=OFF_DUTY_VALUE)
    return out


def _off_duty_replacement(
    ent: Entity, entities: dict[str, Entity], staleness_factor: float
) -> Entity | None:
    if ent.kind is not Kind.COMPONENT or ent.state is not State.MISSED:
        return None
    anchor_id = str(ent.detail.get(DUTY_ANCHOR_KEY) or "").strip()
    if not anchor_id or anchor_id == ent.id:
        return None
    anchor = entities.get(anchor_id)
    if anchor is None or anchor.state is not State.MISSED:
        # The substrate's proof of life is current (or unreadable): the row's
        # own MISSED stands — this is the on-duty miss that must stay loud.
        return None
    anchor_last = _parse(_as_of(anchor))
    row_last = _parse(_as_of(ent))
    cadence = _positive(ent.detail.get(_CADENCE))
    if anchor_last is None or row_last is None or cadence is None:
        return None
    due_by = row_last.timestamp() + cadence * 60.0 * float(staleness_factor)
    if due_by < anchor_last.timestamp():
        # Already overdue while the anchor was still publishing: a miss on
        # duty, which the substrate going down afterwards does not explain.
        return None
    return dataclasses.replace(
        ent,
        state=State.OFF_DUTY,
        detail={
            **ent.detail,
            "off_duty_anchor": anchor_id,
            "off_duty_since": _as_of(anchor),
        },
    )


def _as_of(ent: Entity) -> str | None:
    """The as-of that won the merge (`graph._effective_as_of`'s rule)."""
    return ent.field_sources.get("as_of", ent.provenance).as_of


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _positive(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None
