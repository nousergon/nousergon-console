"""local-units adapter — Component and Run entities from the local scheduler.

Reads the host's systemd inventory — every unit whose name matches a
configured prefix (`config.example.yaml`'s `local-units` block) — and emits
one Component entity per unit plus one Run entity per recorded activation.
This is how "scheduled but not running" and "running but not registered"
become rows the exception-first surface can lead with (§4.3).

The adapter is generic — it knows "a systemd host's unit inventory with a
configured name-prefix selector", nothing about any fleet. Every literal
(the unit prefixes, the unit paths) comes from configuration or the source
itself (§2.3); a topology literal in adapter source is a build finding.

The adapter is hermetic: the systemd read is one injectable function,
``_enumerate``, so tests run over recorded fixtures with no live systemd
(groom-sweep-policy.md §8.1). In production ``_enumerate`` shells to
``systemctl`` — the local scheduler is a source like any other, and an
unreachable one is a FAILED adapter state, never an exception that empties
the surface (§2.3).

State mapping is the source's own statement, never a guessed verdict:

- ``active``            → HEALTHY (the unit is up)
- ``activating`` /
  ``deactivating`` /
  ``reloading``         → DEGRADED (in transition)
- ``failed``            → FAILING (the unit itself says it failed)
- ``masked`` (load)     → ABSENT (the operator explicitly disabled it —
                          systemd supplies the intent)
- ``inactive``          → UNKNOWN (loaded but resting. Whether that is a
                          finding needs the registry's intended state,
                          which an adapter must not read (§2.3) — so the
                          absence is declared, never guessed as ABSENT or
                          HEALTHY)
- no state at all       → UNKNOWN (an unreadable record — a declared
                          absence, never a dropped row)

A unit's recorded activation — its InvocationID, the run id the source
assigns (§2.1) — is a Run entity whose as-of is the activation's start and
whose state is the activation's outcome: a live run is HEALTHY, a failed
unit's run FAILING, a finished activation HEALTHY on ``Result=success`` and
FAILING on any other recorded result. launchd is a future extension; this
adapter reads systemd only.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable

from ..model.entity import Edge, Entity, Provenance
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass
from ..model.kinds import Kind, State

#: Enumerating a substrate is DISCOVERY (§2.5): it establishes that a unit is
#: THERE. It also reads the unit's own ActiveState, which is why its claims
#: still carry a state — but they rank below telemetry, and a `masked` unit's
#: DISABLED is superseded by the merge unless a registry declares it, because
#: §8.3 reserves that state to a declaration.
CLAIM_CLASS = ClaimClass.DISCOVERY

name = "schedules"
produces = ("component", "run")

#: One unit record: the raw `systemctl show --output=json` dict for a unit.
UnitRecord = dict[str, Any]
#: An enumerator takes the configured unit prefixes and returns the in-scope
#: units' records. Injectable for hermetic tests; the default reads live
#: systemd via `systemctl`.
Enumerator = Callable[[list[str]], list[UnitRecord]]


def _systemctl_enumerate(prefixes: list[str]) -> list[UnitRecord]:
    """Read the live systemd inventory: enumerate units, keep the in-scope
    ones, and fetch each one's activation record. Raises when the scheduler
    is unreachable — the adapter turns that into a FAILED state."""
    out = subprocess.run(
        ["systemctl", "list-units", "--all", "--output=json"],
        capture_output=True, text=True, check=True,
    )
    in_scope = [
        u.get("unit") for u in json.loads(out.stdout or "[]")
        if any(str(u.get("unit", "")).startswith(p) for p in prefixes)
    ]
    records: list[UnitRecord] = []
    for unit in in_scope:
        try:
            show = subprocess.run(
                ["systemctl", "show", unit, "--output=json", "--timestamp=unix"],
                capture_output=True, text=True, check=True,
            )
            rec = json.loads(show.stdout or "{}")
        except Exception:
            # An unreadable unit is a declared absence (§2.3): it exists in
            # the inventory but its activation record could not be read, so
            # it stays as a component with no claim about its state.
            rec = {"Id": unit}
        records.append(rec)
    return records


def fetch(config: dict[str, Any], enumerator: Enumerator | None = None) -> AdapterResult:
    prefixes = config.get("unit_prefixes")
    if not isinstance(prefixes, (list, tuple)) or not prefixes:
        # An enabled adapter with no in-scope units declared is a config
        # error, not an empty surface — with no selector we cannot know
        # which units are in scope, so we say so (§2.3).
        return AdapterResult(
            claim_class=CLAIM_CLASS,
            name=config.get("_name", name),
            status=AdapterStatus.FAILED,
            unavailable=("unit_prefixes",),
        )
    enumerator = enumerator or _systemctl_enumerate
    try:
        records = enumerator(list(prefixes))
    except Exception:
        # The scheduler itself is unreachable — a FAILED adapter state with
        # no entities to mark UNREPORTED, never an exception (§2.3).
        return AdapterResult(
            claim_class=CLAIM_CLASS,
            name=config.get("_name", name),
            status=AdapterStatus.FAILED,
            unavailable=("source",),
        )

    entities: list[Entity] = []
    edges: list[Edge] = []
    partial = False
    for rec in records:
        mapped = _to_entities(rec)
        if mapped is None:
            continue  # a record naming no unit is not a unit — nothing to render
        component, run, activation_missing = mapped
        entities.append(component)
        if run is not None:
            entities.append(run)
            # The run belongs to its unit; the index derives the reverse so
            # the unit page can list its activations (§3.3).
            edges.append(Edge(source=run.id, rel="belongs-to", target=component.id))
        if activation_missing:
            partial = True

    return AdapterResult(
        claim_class=CLAIM_CLASS,
        name=config.get("_name", name),
        status=AdapterStatus.OK,
        entities=tuple(entities),
        edges=tuple(edges),
        unavailable=("activation",) if partial else (),
    )


def _to_entities(rec: dict[str, Any]) -> tuple[Entity, Entity | None, bool] | None:
    """Map one unit record to its entities: always a Component, plus a Run
    when the record carries a recorded activation (an InvocationID), plus
    whether the activation record itself was unreadable."""
    unit = str(rec.get("Id") or rec.get("unit") or "")
    if not unit:
        return None
    active = str(rec.get("ActiveState") or "").lower()
    sub = str(rec.get("SubState") or "").lower()
    load = str(rec.get("LoadState") or "").lower()
    fragment = rec.get("FragmentPath")
    activated_at = _as_of(rec.get("ActiveEnterTimestamp"))

    component = Entity(
        kind=Kind.COMPONENT,
        id=unit,
        state=_component_state(active, load),
        provenance=Provenance(
            source="systemd",
            as_of=activated_at,
            evidence=str(fragment) if fragment else None,
        ),
        detail={
            "sub_state": sub,
            "unit_file_state": str(rec.get("UnitFileState") or ""),
            "description": str(rec.get("Description") or ""),
        },
    )

    invocation = str(rec.get("InvocationID") or "")
    if not invocation:
        # No recorded activation — the unit alone, with no run to claim. A
        # record with no state at all is an unreadable one, which we declare.
        return component, None, not active

    result = str(rec.get("Result") or "").lower()
    run = Entity(
        kind=Kind.RUN,
        id=invocation,  # InvocationID — the run id the source assigns (§2.1)
        state=_run_state(active, result),
        provenance=Provenance(
            source="systemd",
            as_of=activated_at,
            evidence=str(fragment) if fragment else None,
        ),
        detail={"unit": unit, "result": result or None},
    )
    return component, run, False


def _component_state(active: str, load: str) -> State:
    """The unit's own statement about itself — never a guessed verdict.

    ``masked`` is the one inactive case with an operator intent the source
    itself supplies, which renders DISABLED; every other inactive unit is
    UNREPORTED because whether it SHOULD be running lives in the registry,
    which an adapter must not read (§2.3)."""
    if load == "masked":
        # The one inactive case carrying an operator intent the SOURCE supplies:
        # masking is a deliberate off-switch, which is §8.3's DISABLED. Not
        # ABSENT — the unit is present, and ABSENT means the substrate lacks it.
        return State.DISABLED
    if active == "active":
        return State.HEALTHY
    if active in ("activating", "deactivating", "reloading"):
        return State.DEGRADED
    if active == "failed":
        return State.FAILED
    # Inactive or unreadable. Whether it SHOULD be running lives in the
    # registry, which an adapter must not read (§2.3) — so this adapter cannot
    # place it, and §8.3's answer for that is UNREPORTED. Loud, and a finding.
    return State.UNREPORTED


def _run_state(active: str, result: str) -> State:
    """An activation's state: a live run is HEALTHY, a failed unit's run
    FAILED, and a finished activation's outcome is the ``Result`` systemd
    recorded — success is HEALTHY, any other recorded outcome FAILED, and no
    recorded outcome at all is NEVER_RAN (§8.3)."""
    if active == "active":
        return State.HEALTHY
    if active == "activating":
        return State.DEGRADED
    if active == "failed":
        return State.FAILED
    if active == "inactive":
        if result == "success":
            return State.HEALTHY
        if result:
            return State.FAILED
        # systemd records no Result before a unit has ever activated. That is
        # §8.3's NEVER_RAN precisely — never executed, therefore never tested,
        # and its first failure is still ahead of it. Distinct from MISSED.
        return State.NEVER_RAN
    return State.UNREPORTED


def _as_of(timestamp: object) -> str | None:
    """systemd `--timestamp=unix` values are epoch seconds; render as ISO-8601
    UTC. Unset (never activated, or systemd's 0) is a declared absence — no
    invented freshness stamp (§2.3)."""
    if timestamp in (None, "", 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None
