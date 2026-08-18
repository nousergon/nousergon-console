"""`sql-query` adapter — Signal / Decision / Run entities from configured
SELECT queries over a SQLite-shaped database (`console-policy.md` §2.3).

Distinct from the `sql-source` **driver** (`console/drivers/sql_source.py`):
that driver reads ONE row bound to ONE already-known component, from a spec
declared in a *component descriptor* (a file committed beside the component,
which in a public repo means the spec itself must never carry a credential —
hence its `credential` indirection). This is the many-row counterpart, used
when a query's rows ARE the entities — a Signal per ticker-date, a Decision
per ticker-eval_date, a Run per pipeline cycle — rather than facts about one
component somebody already registered. Its config lives in the console's own
`config.yaml` (gitignored, §2.3's normal home for topology), so a literal
`db_path` here is the same shape as `object-store`'s literal `bucket` — no
credential indirection needed at this layer.

One adapter, several **named queries**, each declaring its own projection:

    queries:
      - name: cio-decisions
        entity_kind: decision            # signal | decision | run | artifact
        query: "SELECT ticker, eval_date, cio_decision FROM cio_evaluations"
        id_template: "{ticker}:{eval_date}"
        state_field: cio_decision        # raw value, verbatim (§5.1 2nd half)
        as_of_field: eval_date
        facets: {team_id: team_id}
        json_columns: []                 # columns holding a JSON string to decode

Every query is validated as one parameterless `SELECT` — no semicolon, no
second statement — before anything runs (§2.3's "configured, never
hardcoded" extends to "never executable beyond a read").

`entity_kind: run` (and `component`, though no binding in this repo currently
uses it) resolves to `observability-policy.md` §8.3's closed vocabulary,
never a raw string (`model/entity.py`'s `__post_init__` enforces this
structurally) — via `state_map` (raw column value → state name) or
`default_state` (a row's mere presence declares this state; used when a
query enumerates completed cycles with no failure column of their own, so
existence *is* the state fact). Neither is fabricated in adapter source: an
opinion like "a row existing means the cycle ran" belongs in the query
binding's own config, not hardcoded here, so a different deployment's
schema can say something different. A row that resolves neither renders
`UNREPORTED` and is named in `unavailable` — loud, never guessed.

Hermetic: the query execution is one injectable function so tests run over
recorded row fixtures with no live database (groom-sweep §8.1). Production
wiring is stdlib `sqlite3` opened read-only — no optional extra to install,
since SQLite is the shape this adapter exists for.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from ..model.entity import Entity, Provenance
from ..index.build import now_iso
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass
from ..model.kinds import COMPONENT_STATE_KINDS, Kind, State

#: A query result is an OBSERVATION (§2.5): it reports what the database
#: currently holds, a fact about state at read time.
CLAIM_CLASS = ClaimClass.OBSERVATION

name = "sql-query"
produces = ("signal", "decision", "run", "artifact")

Row = dict[str, Any]
#: (db_path, query_text) -> rows, each already a plain dict keyed by column name.
QueryRunner = Callable[[str, str], list[Row]]


def fetch(
    config: dict[str, Any],
    runner: QueryRunner | None = None,
) -> AdapterResult:
    db_path = config.get("db_path")
    queries = config.get("queries") or []
    if not db_path:
        return _failed(config, "db_path")
    if not queries:
        return _failed(config, "queries")

    for q in queries:
        error = _validate_query(str(q.get("query") or ""))
        if error:
            return _failed(config, f"query:{q.get('name', '?')}:{error}")

    if runner is None:
        runner = _default_runner()

    entities: list[Entity] = []
    unavailable: list[str] = []
    read_any = False

    for q in queries:
        qname = str(q.get("name") or "query")
        entity_kind = _kind(q.get("entity_kind"))
        if entity_kind is None:
            unavailable.append(f"{qname}:entity_kind")
            continue
        try:
            rows = runner(str(db_path), str(q["query"]))
        except Exception:  # noqa: BLE001 - a failed source is a state, never an exception (§2.3)
            unavailable.append(qname)
            continue
        read_any = True
        for row in rows:
            entity, ok = _to_entity(row, entity_kind, q, db_path, qname)
            if entity is not None:
                entities.append(entity)
            if not ok:
                unavailable.append(f"{qname}:state")

    if not read_any:
        return _failed(config, "source")

    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        name=config.get("_name", name),
        status=AdapterStatus.OK,
        entities=tuple(entities),
        unavailable=tuple(dict.fromkeys(unavailable)),  # de-duplicate, keep order
    )


def _to_entity(
    row: Row, kind: Kind, q: dict[str, Any], db_path: str, qname: str,
) -> tuple[Entity | None, bool]:
    row = _decode_json_columns(row, q.get("json_columns") or [])
    id_template = q.get("id_template")
    if not id_template:
        return None, True
    try:
        entity_id = str(id_template).format(**row)
    except (KeyError, IndexError):
        return None, True

    facets: dict[str, str] = {}
    for facet_name, column in (q.get("facets") or {}).items():
        value = row.get(column)
        if value is not None:
            facets[str(facet_name)] = str(value)

    as_of = None
    as_of_field = q.get("as_of_field")
    if as_of_field:
        raw_as_of = row.get(as_of_field)
        as_of = str(raw_as_of) if raw_as_of is not None else None

    evidence = None
    evidence_template = q.get("evidence_template")
    if evidence_template:
        try:
            evidence = str(evidence_template).format(**row)
        except (KeyError, IndexError):
            evidence = None

    detail_columns = q.get("detail_columns")
    excluded = set((q.get("facets") or {}).values())
    if detail_columns:
        detail = {str(c): row.get(c) for c in detail_columns}
    else:
        detail = {k: v for k, v in row.items() if k not in excluded}

    state, ok = _resolve_state(row, kind, q)
    entity = Entity(
        kind=kind,
        id=entity_id,
        state=state,
        provenance=Provenance(
            source=f"sql-query:{db_path}:{qname}", as_of=as_of, evidence=evidence,
        ),
        facets=facets,
        detail=detail,
    )
    return entity, ok


def _resolve_state(row: Row, kind: Kind, q: dict[str, Any]) -> tuple[State | str, bool]:
    """§5.1: a Run/Component row MUST resolve to one of §8.3's thirteen; a
    Signal/Decision/Artifact row carries the source's own value verbatim.

    Returns ``(state, ok)`` — ``ok`` is False when a Run/Component row could
    not be placed at all, which the caller folds into `unavailable` (§2.3:
    declared unable, never guessed).
    """
    state_field = q.get("state_field")
    raw = row.get(state_field) if state_field else None

    if kind in COMPONENT_STATE_KINDS:
        if raw is not None:
            state_map = {
                str(k).lower(): str(v).upper() for k, v in (q.get("state_map") or {}).items()
            }
            mapped_name = state_map.get(str(raw).lower())
            if mapped_name and mapped_name in State.__members__:
                return State[mapped_name], True
            return State.UNREPORTED, False
        default_name = q.get("default_state")
        if default_name and str(default_name).upper() in State.__members__:
            return State[str(default_name).upper()], True
        return State.UNREPORTED, False

    if raw is not None:
        return str(raw), True
    # A Signal/Decision/Artifact row's mere presence is data — never a declared
    # absence, so this is not folded into `unavailable`.
    return "reporting", True


def _decode_json_columns(row: Row, columns: list[str]) -> Row:
    if not columns:
        return row
    out = dict(row)
    for col in columns:
        raw = out.get(col)
        if isinstance(raw, str):
            try:
                out[col] = json.loads(raw)
            except (TypeError, ValueError):
                pass  # left as the raw string — a malformed value is still rendered, never dropped
    return out


def _kind(raw: Any) -> Kind | None:
    if not raw:
        return None
    try:
        return Kind(str(raw).lower())
    except ValueError:
        return None


def _validate_query(query: str) -> str | None:
    """Mirrors `model/descriptor.py`'s `sql-source` validation: exactly one
    parameterless SELECT. This adapter's config lives in gitignored
    `config.yaml`, so — unlike a descriptor's spec — a literal `db_path` is
    fine (§2.3, same as `object-store`'s literal `bucket`); the query itself
    is still constrained to a read, never an executable statement."""
    q = query.strip()
    if not q.lower().startswith("select "):
        return "must be a single SELECT statement"
    if ";" in q.rstrip(";"):
        return "must be exactly one statement"
    return None


def _failed(config: dict[str, Any], missing: str) -> AdapterResult:
    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        name=config.get("_name", name),
        status=AdapterStatus.FAILED,
        unavailable=(missing,),
    )


def _default_runner() -> QueryRunner:
    """Read-only stdlib sqlite3 — no optional extra, since SQLite is the
    source shape this adapter exists for."""

    def runner(db_path: str, query: str) -> list[Row]:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            cursor = connection.execute(query)
            return [dict(record) for record in cursor.fetchall()]
        finally:
            connection.close()

    return runner
