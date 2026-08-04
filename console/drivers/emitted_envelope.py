"""`emitted-envelope` driver — a component's own report, where it declared it.

The fallback binding (§2.6): for a module with **nowhere natural to put a
number**, `console/emit.py` writes the versioned envelope and the descriptor
points at it.

    metrics:
      - driver: emitted-envelope
        key: "s3://bucket/reports/my-job/latest.json"

This is deliberately no longer the default path. Most facts are already written
down somewhere, and asking a module to write them a second time is asking it to
maintain two truths — the second of which drifts silently, because nothing
compares them.

What it adds over the `checks-envelope` **adapter**: the adapter enumerates a
prefix the console's config names, so a module whose report lands anywhere else
costs a console edit. Here the component says where its own report is.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from ..model.descriptor import Binding
from ..model.entity import Edge, Entity, Provenance
from ..model.kinds import Kind, State
from .base import Cost, DriverResult

name = "emitted-envelope"
kinds = ("component", "run", "artifact")
cost = Cost.CHEAP


def read(binding: Binding, context: dict[str, Any]) -> DriverResult:
    key = binding.spec.get("key")
    if not key:
        return DriverResult.failed(
            binding, "no `key` declared — say where the envelope is written",
            unavailable=("key",),
        )

    reader: Callable[[str], Any] | None = context.get("document_reader")
    if reader is None:
        return DriverResult.failed(
            binding,
            "no document reader available — the binding is declared and "
            "unreadable, which is a different finding from the envelope being "
            "absent",
            unavailable=("reader",),
        )

    try:
        raw = reader(str(key))
    except FileNotFoundError:
        return DriverResult.failed(
            binding, f"no envelope at {key} — declared and never written",
            unavailable=("envelope",),
        )
    except Exception as exc:  # noqa: BLE001 - a state, never an exception
        return DriverResult.failed(binding, f"{type(exc).__name__}: {exc}")

    body = raw if isinstance(raw, dict) else _parse(raw)
    if body is None:
        return DriverResult.failed(
            binding,
            f"the envelope at {key} did not parse as JSON — an emitter writing "
            "a half-flushed document renders as a broken component, so this "
            "says so rather than guessing",
            unavailable=("body",),
        )

    now = context.get("now") or datetime.now(timezone.utc)
    cadence = _cadence_seconds(body)
    ran_at = body.get("ran_at")
    state = _state(str(body.get("status") or "").lower(), ran_at, cadence,
                   float(context.get("staleness_factor", 1.5)), now)

    component = Entity(
        kind=Kind.COMPONENT,
        id=binding.component_id,  # the descriptor's id wins — one namespace (§3.6)
        state=state,
        provenance=Provenance(
            source=f"emitted-envelope:{key}",
            as_of=str(ran_at) if ran_at else None,
            evidence=str(body.get("deep_link") or key),
        ),
        detail={
            "summary": body.get("summary"),
            "status": body.get("status"),
            "cadence_seconds": cadence,
            # §5.8's descriptors travel verbatim and uninterpreted — the whole
            # point is that no path is keyed on who emitted them.
            "fields": body.get("fields") or {},
        },
    )

    edges: list[Edge] = []
    for produced in body.get("produces") or []:
        edges.append(Edge(source=binding.component_id, rel="produces",
                          target=str(produced)))
    for consumed in body.get("consumes") or []:
        edges.append(Edge(source=str(consumed), rel="consumed-by",
                          target=binding.component_id))

    return DriverResult(binding=binding, entities=(component,),
                        edges=tuple(edges), cadence_seconds=cadence)


def _parse(raw: Any) -> dict | None:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _cadence_seconds(body: dict) -> float | None:
    raw = body.get("cadence_minutes")
    try:
        return float(raw) * 60.0 if raw else None
    except (TypeError, ValueError):
        return None


def _state(status: str, ran_at: Any, cadence: float | None,
           factor: float, now: datetime) -> State:
    """Status first, then staleness — the envelope adapter's rule, unchanged.

    Past its cadence the component is `MISSED`: the schedule fired or should
    have and no run started, which is a failure upstream of the component in
    its trigger. Not `STALLED` (nothing reported a start), not `FAILED` (it did
    not stop, it never began).
    """
    base = {
        "ok": State.HEALTHY,
        "attention": State.DEGRADED,
        "error": State.FAILED,
    }.get(status, State.UNREPORTED)
    if ran_at is None or not cadence:
        return base
    try:
        ts = datetime.fromisoformat(str(ran_at).replace("Z", "+00:00"))
    except ValueError:
        return base
    if (now - ts).total_seconds() > cadence * factor:
        return State.MISSED
    return base
