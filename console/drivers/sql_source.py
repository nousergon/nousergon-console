"""`sql-source` driver — read descriptor-declared metrics from a query result."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from ..model.descriptor import Binding
from ..model.entity import Entity, Provenance
from ..model.kinds import Kind, State
from .base import Cost, DriverResult

name = "sql-source"
kinds = ("component",)
cost = Cost.METERED


def read(binding: Binding, context: dict[str, Any]) -> DriverResult:
    credential = binding.spec.get("credential")
    query = binding.spec.get("query")
    if not credential or not query:
        return DriverResult.failed(binding, "sql-source requires `credential` and `query`", ("binding",))
    credentials = context.get("sql_credentials") or {}
    target = credentials.get(str(credential))
    if not target:
        return DriverResult.failed(binding, f"credential reference {credential!r} is unavailable", ("credential",))
    timeout = float(binding.spec.get("timeout_seconds", 5))
    connect: Callable[[str, float], Any] = context.get("sql_connect") or _connect
    try:
        connection = connect(str(target), timeout)
        try:
            cursor = connection.execute(str(query))
            row = cursor.fetchone()
            columns = tuple(column[0] for column in cursor.description or ())
        finally:
            connection.close()
    except Exception as exc:  # noqa: BLE001 - a failed source remains a named finding
        return DriverResult.failed(binding, f"{type(exc).__name__}: {exc}")
    source = f"sql-source:{credential}"
    provenance = Provenance(source=source, as_of=datetime.now(timezone.utc).isoformat(), evidence=None)
    if row is None:
        return _finding(binding, provenance, "empty-result")
    values = dict(zip(columns, row, strict=True))
    field = binding.spec.get("field")
    if field and field not in values:
        return _finding(binding, provenance, "missing-column", field=str(field), columns=columns)
    fields = {str(field): values[str(field)]} if field else values
    return DriverResult(binding=binding, entities=(Entity(kind=Kind.COMPONENT, id=binding.component_id,
        state=State.HEALTHY, provenance=provenance, detail={"fields": fields}),), cadence_seconds=None)


def _finding(binding: Binding, provenance: Provenance, condition: str, **detail: Any) -> DriverResult:
    return DriverResult(binding=binding, entities=(Entity(kind=Kind.COMPONENT, id=binding.component_id,
        state=State.DEGRADED, provenance=provenance,
        detail={"sql_source": {"condition": condition, **detail}}),))


def _connect(target: str, timeout: float):
    return sqlite3.connect(target, timeout=timeout)
