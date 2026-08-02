"""state-machine adapter — Run / Cycle / Artifact entities from a state machine.

Reads a configured list of state-machine ARNs (Step Functions shape or any
source that can project the same execution records) and emits one Run per
execution, one Cycle per distinct value of the configured ``cycle_key``, and
Artifact edges where an execution's input/output names durable keys
(`config.example.yaml`'s `state-machine` block).

Generic over "a state machine whose executions carry an optional cycle key and
optional durable-key references" — region, ARNs and the cycle key all come from
configuration (§2.3). The reader is one injectable function so tests run over
recorded execution lists with no live AWS (groom-sweep §8.1).

Horizon honesty (§3.4): the adapter pages **fully** and records the covered
horizon in ``detail`` and ``unavailable`` rather than silently truncating. A
silently truncated execution history is the forbidden shape at the data layer.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from ..model.entity import Edge, Entity, Provenance
from ..model.envelope import AdapterResult, AdapterStatus
from ..model.kinds import Kind, State

name = "pipelines"
produces = ("run", "cycle", "artifact")

#: One execution record as the reader returns it. Keys mirror the SF list +
#: describe shape so a production reader can pass boto3 responses through with
#: minimal mapping, but the adapter never imports a vendor SDK.
ExecutionRecord = dict[str, Any]
#: A reader takes (region, state_machine_arn) and returns every execution the
#: source will yield for that machine — fully paged. Injectable.
ExecutionReader = Callable[[str, str], list[ExecutionRecord]]


def fetch(
    config: dict[str, Any],
    reader: ExecutionReader | None = None,
) -> AdapterResult:
    region = config.get("region")
    machines = list(config.get("state_machines") or [])
    cycle_key = config.get("cycle_key") or "execution_date"
    if not region:
        return AdapterResult(
            name=config.get("_name", name),
            status=AdapterStatus.FAILED,
            unavailable=("region",),
        )
    if not machines:
        return AdapterResult(
            name=config.get("_name", name),
            status=AdapterStatus.FAILED,
            unavailable=("state_machines",),
        )
    if reader is None:
        # No production SF client is bundled; a deployment injects one (boto3).
        # Absent a client the adapter declares itself unable rather than
        # silently returning zero rows — absence renders as itself (§5.5).
        return AdapterResult(
            name=config.get("_name", name),
            status=AdapterStatus.FAILED,
            unavailable=("reader",),
        )

    entities: list[Entity] = []
    edges: list[Edge] = []
    cycles_seen: dict[str, list[str]] = {}  # cycle_id → run ids belonging to it
    failed = False
    machines_read = 0
    earliest: str | None = None
    latest: str | None = None
    total_executions = 0

    for arn in machines:
        try:
            records = reader(region, arn)
        except Exception:
            failed = True
            continue
        machines_read += 1
        for rec in records:
            mapped = _to_entities(rec, arn, region, cycle_key)
            if mapped is None:
                continue
            run, cycle_id, arts, run_edges = mapped
            entities.append(run)
            total_executions += 1
            as_of = run.provenance.as_of
            if as_of:
                if earliest is None or as_of < earliest:
                    earliest = as_of
                if latest is None or as_of > latest:
                    latest = as_of
            if cycle_id:
                cycles_seen.setdefault(cycle_id, []).append(run.id)
                edges.append(Edge(source=run.id, rel="belongs-to", target=cycle_id))
            for art in arts:
                entities.append(art)
            edges.extend(run_edges)

    # One Cycle entity per distinct cycle key value. The cycle's state is the
    # worst of its runs: any FAILING run makes the cycle FAILING; else DEGRADED
    # if any is; else HEALTHY if any succeeded; else UNKNOWN.
    run_by_id = {e.id: e for e in entities if e.kind is Kind.RUN}
    for cycle_id, run_ids in cycles_seen.items():
        states = [run_by_id[r].state for r in run_ids if r in run_by_id]
        entities.append(Entity(
            kind=Kind.CYCLE,
            id=cycle_id,
            state=_worst(states),
            provenance=Provenance(
                source=f"state-machine:{region}",
                as_of=latest,
                evidence=None,
            ),
            detail={"run_count": len(run_ids), "cycle_key": cycle_key},
        ))

    unavailable: list[str] = ["cost"]  # SF executions carry no cost tag by default
    if failed and machines_read == 0:
        return AdapterResult(
            name=config.get("_name", name),
            status=AdapterStatus.FAILED,
            unavailable=("source",),
        )
    if failed:
        unavailable.append("partial-source")
    if total_executions == 0:
        unavailable.append("executions")

    # Horizon is a first-class declaration so a truncated history cannot pass
    # as complete (§3.4). Stamped onto every Cycle so the covered window is
    # observable on the surface rather than inferred from the run list.
    entities = [
        _with_horizon(e, earliest, latest, total_executions, machines_read, len(machines))
        if e.kind is Kind.CYCLE else e
        for e in entities
    ]

    return AdapterResult(
        name=config.get("_name", name),
        status=AdapterStatus.OK,
        entities=tuple(entities),
        edges=tuple(edges),
        unavailable=tuple(unavailable),
    )


def _with_horizon(
    entity: Entity,
    earliest: str | None,
    latest: str | None,
    total: int,
    machines_read: int,
    machines_configured: int,
) -> Entity:
    """Stamp the covered horizon onto a Cycle so truncation is observable."""
    detail = dict(entity.detail)
    detail["horizon_earliest"] = earliest
    detail["horizon_latest"] = latest
    detail["horizon_execution_count"] = total
    detail["horizon_machines_read"] = machines_read
    detail["horizon_machines_configured"] = machines_configured
    detail["horizon_complete"] = machines_read == machines_configured
    return Entity(
        kind=entity.kind,
        id=entity.id,
        state=entity.state,
        provenance=entity.provenance,
        facets=dict(entity.facets),
        detail=detail,
    )


def _to_entities(
    rec: dict[str, Any],
    arn: str,
    region: str,
    cycle_key: str,
) -> tuple[Entity, str | None, list[Entity], list[Edge]] | None:
    """Map one execution record → Run + optional cycle id + artifacts + edges."""
    execution_arn = str(rec.get("executionArn") or rec.get("arn") or "")
    execution_name = str(rec.get("name") or "")
    run_id = execution_arn or execution_name
    if not run_id:
        return None

    status = str(rec.get("status") or "").upper()
    start = _as_of(rec.get("startDate"))
    stop = _as_of(rec.get("stopDate"))
    input_obj = _parse_jsonish(rec.get("input"))
    output_obj = _parse_jsonish(rec.get("output"))

    cycle_id = None
    if isinstance(input_obj, dict):
        raw = input_obj.get(cycle_key)
        if raw is not None and str(raw):
            cycle_id = str(raw)

    run = Entity(
        kind=Kind.RUN,
        id=run_id,  # source-assigned (§2.1) — the execution ARN
        state=_run_state(status),
        provenance=Provenance(
            source=f"state-machine:{arn}",
            as_of=stop or start,
            evidence=execution_arn or None,
        ),
        facets={"substrate": "state-machine"},
        detail={
            "status": status or None,
            "name": execution_name or None,
            "state_machine_arn": arn,
            "region": region,
            "start": start,
            "stop": stop,
            "cycle_id": cycle_id,
        },
    )

    arts: list[Entity] = []
    edges: list[Edge] = []
    for key in _durable_keys(input_obj) + _durable_keys(output_obj):
        arts.append(Entity(
            kind=Kind.ARTIFACT,
            id=key,
            state=State.UNKNOWN,  # existence known; freshness is another adapter's job
            provenance=Provenance(
                source=f"state-machine:{arn}",
                as_of=stop or start,
                evidence=execution_arn or None,
            ),
            detail={"named_by_run": run_id},
        ))
        edges.append(Edge(source=run_id, rel="produces", target=key))

    return run, cycle_id, arts, edges


def _durable_keys(obj: Any) -> list[str]:
    """Pull s3:// URIs and explicit artifact-key fields out of input/output.

    Conservative: only values that are clearly durable references, never every
    string. Walks one level of dict/list so a typical SF payload is covered
    without a full JSONPath dependency.
    """
    found: list[str] = []
    if obj is None:
        return found
    if isinstance(obj, str):
        if obj.startswith("s3://"):
            found.append(obj)
        return found
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and v.startswith("s3://"):
                found.append(v)
            elif isinstance(v, str) and k in ("artifact_key", "artifact", "key", "output_key", "input_key") and v:
                found.append(v)
            elif isinstance(v, (dict, list)):
                found.extend(_durable_keys(v))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_durable_keys(item))
    return found


def _run_state(status: str) -> State:
    """Map the source's own execution status onto the closed vocabulary.

    The source's statement, never a guessed verdict (§2.3): SUCCEEDED is
    HEALTHY, FAILED/TIMED_OUT/ABORTED are FAILING, RUNNING/PENDING are
    DEGRADED, anything else (including missing) is UNKNOWN.
    """
    if status == "SUCCEEDED":
        return State.HEALTHY
    if status in ("FAILED", "TIMED_OUT", "ABORTED"):
        return State.FAILING
    if status in ("RUNNING", "PENDING", "PENDING_REDRIVE"):
        return State.DEGRADED
    return State.UNKNOWN


def _worst(states: list[State]) -> State:
    order = (
        State.FAILING, State.FAILED, State.DEGRADED, State.STALE,
        State.UNREPORTED, State.UNKNOWN, State.ABSENT, State.NEVER_RAN,
        State.NOT_MEASURED, State.HEALTHY,
    )
    for candidate in order:
        if candidate in states:
            return candidate
    return State.UNKNOWN


def _parse_jsonish(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _as_of(value: Any) -> str | None:
    """Normalise startDate/stopDate — datetime, epoch, or ISO string — to ISO-8601 UTC."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        ts = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).isoformat()
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return value  # already a stamp we cannot parse — pass through
    return None
