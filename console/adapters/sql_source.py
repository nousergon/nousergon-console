"""`sql-source` adapter — many rows, one query, many entities (§2.3, §2.5).

`drivers/sql_source.py` is the **driver**: one component's descriptor names
one parameterless `SELECT`, and the single returned row becomes that
component's own metrics (§2.7 — "which table belongs to whom is in the
descriptor"). Two of this slice's sources
(`nousergon-console#58`: `50_Data_Integrity`, `Data_and_Maturity`) are the
same source *shape* — a local SQL query, no live AWS — read the opposite
direction: the console's own config names a query that returns **many** rows,
each becoming one **Signal** keyed by its own composite identifier
((phase, ticker) / (optimizer name, accrual metric)), because neither
question belongs to any single component's descriptor. This is exactly how
`object-store` already exists as both an adapter and a driver over "same
source shape, opposite direction" (`adapters/pipeline_reliability.py`'s own
precedent) — so does `sql-source`, now.

A recorded observation is what actually happened, so this is an OBSERVATION
claim (§2.5), unlike the DECLARATION-class `declared-registry` adapter.

**Self-describing, not bespoke (§5.8).** Every column beyond the identifier
and (optional) state becomes a declared field, rendered from its own
descriptor with no per-query rendering code. A numeric column with no
declared unit renders — and says it has no unit — rather than being silently
interpreted; `field_descriptors` in config is where a deployment supplies
`unit`/`baseline`/`render` for a column that needs one.

**A Signal can name the Component it measures (§3.3, §6).** When a row
carries the id of the component the row is *about* — e.g. a `process_id`
column for an SLA hit-rate row — declaring `component_id_field` derives a
`measures` edge from the Signal to that Component. The reverse (`measured-by`)
is derived by the index, so the Signal appears as a related entity — a
**facet** — on the Component's own entity page with no new rendering path.
This is `54_Fleet_SLA`'s whole migration: a Signal-kind row that names its
Component, nothing else.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from ..model.entity import Edge, Entity, Provenance
from ..index.build import now_iso
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass
from ..model.kinds import Kind

#: A recorded query result is telemetry — what was actually observed, not a
#: statement of what should exist (§2.5).
CLAIM_CLASS = ClaimClass.OBSERVATION

name = "sql-source"
produces = ("signal", "decision", "artifact", "cycle", "incident", "component", "run")

#: A connector takes the configured `database` string and returns a DB-API
#: connection (`.execute` / `.close`). Injectable so tests run over a fixture
#: sqlite file with no live database (groom-sweep §8.1).
Connector = Callable[[str], Any]


def fetch(
    config: dict[str, Any],
    connect: Connector | None = None,
    now: datetime | None = None,
) -> AdapterResult:
    database = config.get("database")
    query = config.get("query")
    kind_name = config.get("kind")
    kind = Kind.from_route(str(kind_name)) if kind_name else None
    id_fields = _string_list(config.get("id_fields"))

    missing = []
    if not database:
        missing.append("database")
    if not query:
        missing.append("query")
    if kind is None:
        missing.append("kind")
    if not id_fields:
        missing.append("id_fields")
    if missing:
        return _failed(config, tuple(missing))

    connect = connect or _default_connect
    try:
        connection = connect(str(database))
        try:
            cursor = connection.execute(str(query))
            columns = tuple(c[0] for c in cursor.description or ())
            rows = cursor.fetchall()
        finally:
            connection.close()
    except Exception:  # noqa: BLE001 - a failed source is a state, never a crash (§2.3)
        return _failed(config, ("source",))

    id_separator = config.get("id_separator", ":")
    state_field = config.get("state_field")
    default_state = config.get("default_state", "reporting")
    component_id_field = config.get("component_id_field")
    field_descriptors = config.get("field_descriptors") or {}
    now = now or datetime.now(timezone.utc)
    fetched_at = now_iso()
    source_label = f"sql-source:{database}"

    excluded = set(id_fields)
    if state_field:
        excluded.add(state_field)
    if component_id_field:
        excluded.add(component_id_field)

    entities: list[Entity] = []
    edges: list[Edge] = []
    skipped = 0

    for row in rows:
        values = dict(zip(columns, row))
        id_parts = [values.get(f) for f in id_fields]
        if any(part is None for part in id_parts):
            skipped += 1
            continue
        eid = id_separator.join(str(part) for part in id_parts)
        state = (
            str(values[state_field])
            if state_field and values.get(state_field) is not None
            else default_state
        )
        entities.append(Entity(
            kind=kind,
            id=eid,
            state=state,
            provenance=Provenance(source=source_label, as_of=fetched_at, evidence=None),
            detail={"fields": _fields(values, excluded, field_descriptors)},
        ))
        if component_id_field:
            cid = values.get(component_id_field)
            if cid:
                edges.append(Edge(source=eid, rel="measures", target=str(cid)))

    unavailable = ("invalid-rows",) if skipped else ()
    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=fetched_at,
        name=config.get("_name", name),
        status=AdapterStatus.OK,
        entities=tuple(entities),
        edges=tuple(edges),
        unavailable=unavailable,
    )


def _failed(config: dict[str, Any], unavailable: tuple[str, ...]) -> AdapterResult:
    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        name=config.get("_name", name),
        status=AdapterStatus.FAILED,
        unavailable=unavailable,
    )


def _fields(
    values: dict[str, Any], excluded: set[str], descriptors: dict[str, Any],
) -> dict[str, Any]:
    """Every non-identifier column, as a §5.8 declared field.

    A deployment's own `field_descriptors[col]` (unit/baseline/render)
    overrides the bare default; without one the field still renders — as
    itself, undecorated — never dropped (§5.8's own rule, not a special case
    here).
    """
    out: dict[str, Any] = {}
    for col, val in values.items():
        if col in excluded:
            continue
        spec = dict(descriptors.get(col) or {})
        spec.setdefault("value", val)
        spec.setdefault("render", "value")
        out[col] = spec
    return out


def _string_list(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value if v)


def _default_connect(database: str) -> Any:
    return sqlite3.connect(database)
