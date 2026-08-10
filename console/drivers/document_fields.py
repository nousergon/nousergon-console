"""`document-fields` driver — declared fields projected from one or more JSON
documents a component's own descriptor points at (§2.7).

The shape: a legacy or ad-hoc JSON artifact whose producer this console does
not control and which was never written in the console's own self-describing
envelope (`console/emit.py`). Distinct from `emitted-envelope`, which assumes
the body already carries the versioned ``{status, ran_at, fields}`` shape —
this driver instead takes the field map FROM THE DESCRIPTOR and extracts
named values out of whatever the document actually contains:

    metrics:
      - driver: document-fields
        documents:
          - key: "s3://bucket/reports/latest.json"
            fields:
              model_version: {path: model_version, render: text}
              hit_rate: {path: hit_rate_30d_rolling, unit: ratio, baseline: 0.55, render: ratio}
          - key: "s3://bucket/reports/manifest.json"
            fields:
              promoted: {path: promoted, render: text}
        cadence_minutes: 1440

**Why `documents` is a list read by ONE binding, not several bindings each
reading one document.** The index's claim-merge (§2.5) only resolves
conflicts at the top-level `detail` key. Two same-rank OBSERVATION claims for
one component, each proposing its own `fields` dict, would collide on the
`fields` key the moment the two documents cover different field names — a
false `DEGRADED` manufactured by the merge layer for the ordinary case of "two
files describe one component." Reading every document a component names
inside one driver call keeps the combination inside the binding the
descriptor author wrote, which is the only place that legitimately knows how
its own sources relate — never a second, accidental precedence rule invented
at the index layer.

Reuses two console-level facilities already established rather than growing a
third: `document_reader` (content — the same injection point
`emitted-envelope` uses) and `object_stat` (freshness — the same injection
point `object-store` uses). No new context plumbing (§2.7).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from ..model.descriptor import Binding
from ..model.entity import Entity, Provenance
from ..model.kinds import Kind, State
from .base import Cost, DriverResult

name = "document-fields"
kinds = ("component",)
cost = Cost.CHEAP


def read(binding: Binding, context: dict[str, Any]) -> DriverResult:
    documents = binding.spec.get("documents")
    if not documents:
        return DriverResult.failed(
            binding,
            "document-fields requires a non-empty `documents` list",
            unavailable=("binding",),
        )

    reader: Callable[[str], Any] | None = context.get("document_reader")
    if reader is None:
        return DriverResult.failed(
            binding,
            "no document reader available — the binding is declared and "
            "unreadable, which is a different finding from the document "
            "being absent",
            unavailable=("reader",),
        )
    stat: Callable[[str], str | None] | None = context.get("object_stat")

    fields: dict[str, Any] = {}
    sources: list[str] = []
    stamps: list[str] = []

    for doc in documents:
        key = doc.get("key")
        if not key:
            return DriverResult.failed(
                binding, "a `documents` entry declares no `key`",
                unavailable=("binding",),
            )
        sources.append(str(key))

        try:
            raw = reader(str(key))
        except FileNotFoundError:
            return DriverResult.failed(
                binding, f"no document at {key} — declared and never written",
                unavailable=("document",),
            )
        except Exception as exc:  # noqa: BLE001 - a state, never an exception (§2.3)
            return DriverResult.failed(binding, f"{type(exc).__name__}: {exc}")

        body = raw if isinstance(raw, dict) else _parse(raw)
        if body is None:
            return DriverResult.failed(
                binding,
                f"the document at {key} did not parse as JSON — a producer "
                "writing a half-flushed file renders as a broken component, "
                "so this says so rather than guessing",
                unavailable=("body",),
            )

        field_map = doc.get("fields")
        if field_map:
            for out_name, spec in field_map.items():
                value = _extract(body, spec.get("path", out_name))
                entry: dict[str, Any] = {"value": value}
                if "unit" in spec:
                    entry["unit"] = spec["unit"]
                if "baseline" in spec:
                    entry["baseline"] = spec["baseline"]
                if "render" in spec:
                    entry["render"] = spec["render"]
                fields[out_name] = entry
        else:
            # No field map declared for this document — the whole body
            # renders opaque and is counted, never dropped (§5.8's
            # "undeclared field" outcome) rather than silently discarded.
            for out_name, value in body.items():
                fields.setdefault(out_name, value)

        if stat is not None:
            try:
                stamp = stat(str(key))
            except Exception:  # noqa: BLE001 - freshness is best-effort here
                stamp = None
            if stamp:
                stamps.append(stamp)

    cadence = _cadence_seconds(binding.spec)
    now = context.get("now") or datetime.now(timezone.utc)
    as_of = max(stamps) if stamps else None
    state = _state(
        as_of, cadence,
        float(binding.spec.get("staleness_factor", context.get("staleness_factor", 1.5))),
        now,
    )

    component = Entity(
        kind=Kind.COMPONENT,
        id=binding.component_id,  # the descriptor's id wins — one namespace (§3.6)
        state=state,
        provenance=Provenance(
            source=f"document-fields:{','.join(sources)}",
            as_of=as_of,
            evidence=sources[0] if sources else None,
        ),
        detail={"fields": fields, "cadence_seconds": cadence},
    )
    return DriverResult(
        binding=binding, entities=(component,), cadence_seconds=cadence,
        unavailable=() if as_of else ("as_of",),
    )


def _extract(body: dict[str, Any], path: str) -> Any:
    """Dotted-path lookup into a nested JSON document: `a.b.c` -> body[a][b][c].

    Returns None for any missing hop rather than raising — a producer's own
    document shape drifting is a rendered defect on the field (§5.8's `parse`
    already handles a None value honestly), never a crash that empties the
    entity.
    """
    cur: Any = body
    for part in str(path).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _parse(raw: Any) -> dict | None:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _cadence_seconds(spec: dict[str, Any]) -> float | None:
    for key, mult in (("cadence_seconds", 1.0), ("cadence_minutes", 60.0),
                      ("cadence_hours", 3600.0)):
        raw = spec.get(key)
        if raw:
            try:
                return float(raw) * mult
            except (TypeError, ValueError):
                return None
    return None


def _state(as_of: str | None, cadence: float | None, factor: float,
           now: datetime) -> State:
    """Freshness-only state — this driver knows nothing of a component's
    business thresholds (§2.3: a driver must not encode a component's own
    logic). A field-level `baseline` (§5.8) is where a business threshold
    belongs; this only says whether the read itself is current.
    """
    if as_of is None or cadence is None:
        return State.HEALTHY  # the declared read succeeded; nothing to compare
    try:
        ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        return State.HEALTHY
    if (now - ts).total_seconds() > cadence * factor:
        return State.MISSED
    return State.HEALTHY
