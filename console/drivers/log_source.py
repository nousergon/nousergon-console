"""`log-source` driver — reads one declared JSON-record log location (§2.7)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from ..model.descriptor import Binding
from ..model.entity import Entity, Provenance
from ..model.kinds import Kind, State
from .base import Cost, DriverResult

name = "log-source"
kinds = ("component",)
cost = Cost.METERED


def read(binding: Binding, context: dict[str, Any]) -> DriverResult:
    location = binding.spec.get("location")
    field = binding.spec.get("field")
    if not location or not field:
        return DriverResult.failed(binding, "log-source requires `location` and `field`", ("binding",))
    window = int(binding.spec.get("window_minutes", 60))
    reader: Callable[[str, int], list[str]] | None = context.get("log_records")
    if reader is None:
        reader = _default_reader(str(binding.spec.get("mechanism") or ""))
    if reader is None:
        return DriverResult.failed(binding, "no log reader available (install the aws extra or inject log_records)", ("reader",))
    try:
        records = reader(str(location), window)
    except Exception as exc:  # noqa: BLE001
        return DriverResult.failed(binding, f"{type(exc).__name__}: {exc}")
    parsed = []
    unstructured = 0
    for raw in records:
        try:
            parsed.append(json.loads(raw) if isinstance(raw, str) else raw)
        except (TypeError, ValueError):
            unstructured += 1
    value = next((row[field] for row in reversed(parsed) if isinstance(row, dict) and field in row), None)
    if value is None:
        condition = "window-empty" if not records else "field-absent"
        return DriverResult(binding=binding, entities=(Entity(kind=Kind.COMPONENT, id=binding.component_id,
            state=State.DEGRADED, provenance=Provenance(source=f"log-source:{location}", evidence=str(location)),
            detail={"log_source": {"condition": condition, "field": str(field),
                                    "structured_records": len(parsed), "unstructured_records": unstructured}}),),
            cadence_seconds=window * 60)
    return DriverResult(binding=binding, entities=(Entity(kind=Kind.COMPONENT, id=binding.component_id,
        state=State.HEALTHY, provenance=Provenance(source=f"log-source:{location}", as_of=datetime.now(timezone.utc).isoformat(), evidence=str(location)),
        detail={"fields": {str(field): value}}),), cadence_seconds=window * 60)


def _default_reader(mechanism: str) -> Callable[[str, int], list[str]] | None:
    """Optional AWS readers; descriptors provide every source instance."""
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError:
        return None
    if mechanism in ("lambda_cloudwatch", "cloudwatch"):
        client = boto3.client("logs")
        def cloudwatch(group: str, window: int) -> list[str]:
            import time
            end = int(time.time() * 1000)
            page = client.filter_log_events(logGroupName=group, startTime=end - window * 60_000, endTime=end)
            return [event["message"] for event in page.get("events", [])]
        return cloudwatch
    if mechanism in ("krepis.ssm_log_capture", "s3"):
        client = boto3.client("s3")
        def s3(uri: str, _window: int) -> list[str]:
            bucket, _, prefix = uri.removeprefix("s3://").partition("/")
            listed = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
            out: list[str] = []
            for obj in listed.get("Contents", []):
                out.extend(client.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().decode("utf-8").splitlines())
            return out
        return s3
    return None
