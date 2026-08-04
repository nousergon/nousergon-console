"""`object-store` driver — one component's artifact, at a key it declares.

The shape: a durable object with a last-modified stamp. The component's
descriptor says which key and how often it is expected to change; this knows
nothing else and must never learn anything else.

    artifacts:
      - driver: object-store
        key: "s3://bucket/reports/latest.parquet"
        cadence_minutes: 1440

Distinct from the `object-store` **adapter**, which enumerates a whole prefix
the console's own config names. Same source shape, opposite direction: the
adapter asks "what is under this prefix", the driver asks "how is this
component's artifact doing". The adapter's prefix is per-source and costs a
console edit per new location; this costs nothing, because the component
declares its own.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ..model.descriptor import Binding
from ..model.entity import Edge, Entity, Provenance
from ..model.kinds import Kind
from .base import Cost, DriverResult

name = "object-store"
kinds = ("artifact",)
cost = Cost.CHEAP  # a head/stat call, not a scan


def read(binding: Binding, context: dict[str, Any]) -> DriverResult:
    key = binding.spec.get("key")
    if not key:
        return DriverResult.failed(
            binding,
            "no `key` declared — an object-store binding must say which object",
            unavailable=("key",),
        )

    stat: Callable[[str], str | None] | None = context.get("object_stat")
    if stat is None:
        stat = _default_stat()
    if stat is None:
        return DriverResult.failed(
            binding,
            "no object-store reader available (install the `aws` extra, or "
            "inject one) — the binding is declared and unreadable, which is a "
            "different finding from the object being missing",
            unavailable=("reader",),
        )

    try:
        last_modified = stat(str(key))
    except Exception as exc:  # noqa: BLE001 - a state, never an exception (§2.3)
        return DriverResult.failed(binding, f"{type(exc).__name__}: {exc}")

    cadence = _cadence_seconds(binding.spec)
    now = context.get("now") or datetime.now(timezone.utc)
    state = _freshness(last_modified, cadence, float(
        binding.spec.get("staleness_factor", context.get("staleness_factor", 1.5))
    ), now)

    artifact = Entity(
        kind=Kind.ARTIFACT,
        id=str(key),  # the key is the source-assigned identifier (§2.1)
        state=state,
        provenance=Provenance(
            source=f"object-store:{key}",
            as_of=last_modified,
            evidence=str(key),
        ),
        detail={
            "declared_by": binding.component_id,
            "cadence_seconds": cadence,
        },
    )
    # The component declared this artifact, so the produces edge is a fact the
    # descriptor established — not an inference from a key pattern (§6).
    edge = Edge(source=binding.component_id, rel="produces", target=str(key))
    return DriverResult(
        binding=binding, entities=(artifact,), edges=(edge,),
        cadence_seconds=cadence,
        unavailable=() if last_modified else ("as_of",),
    )


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


def _freshness(last_modified: str | None, cadence: float | None,
               factor: float, now: datetime) -> str:
    """§5.1's second half: an artifact does not resolve to a component state.

    The three not-computable cases stay three facts (§5.5) — no stamp, no
    declared cadence and an unparseable stamp are different findings with
    different fixes, and collapsing them loses the fix.
    """
    if last_modified is None:
        return "absent"
    if cadence is None:
        return "no-cadence-declared"
    try:
        ts = datetime.fromisoformat(str(last_modified).replace("Z", "+00:00"))
    except ValueError:
        return "unreadable"
    return "fresh" if (now - ts).total_seconds() <= cadence * factor else "stale"


def _default_stat() -> Callable[[str], str | None] | None:
    """boto3-backed head_object, when the optional `aws` extra is installed."""
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError:
        return None

    client = boto3.client("s3")

    def stat(uri: str) -> str | None:
        if not uri.startswith("s3://"):
            raise ValueError(f"not an s3:// URI: {uri!r}")
        bucket, _, key = uri[len("s3://"):].partition("/")
        try:
            head = client.head_object(Bucket=bucket, Key=key)
        except client.exceptions.ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return None  # declared and not there — a finding, not an error
            raise
        return head["LastModified"].isoformat()

    return stat
