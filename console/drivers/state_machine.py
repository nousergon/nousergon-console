"""`state-machine` driver — one component's own execution history.

The driver-direction twin of `console/adapters/state_machine.py` (§2.7's
adapter/driver pair, same precedent as `object-store` and `s3-records`
existing as both): the adapter reads a console-configured LIST of state
machines and builds Run + Cycle + Artifact entities with full horizon-honesty
paging; this driver reads ONE state machine a component's own descriptor
names, and emits Run entities only.

    runs:
      - driver: state-machine
        state_machine_arn: "arn:aws:states:us-east-1:000000000000:stateMachine:weekly-sf"
        region: us-east-1
        cadence_minutes: 1440

Unlike the adapter, there is no Cycle/Artifact/horizon-honesty machinery here
(`nousergon-console#99`) — that is the adapter's job for a configured list of
state machines across the fleet; this driver is one component's own
execution history, and a component descriptor has nowhere to declare a
`cycle_key` or durable-key field names the way the adapter's console config
does. A component needing those needs the adapter's console-config entry, not
this driver.

**The status mapping itself is not reimplemented.** Both this driver and the
`state-machine` adapter import it from `console/state_machine_shape.py::run_state`
— one function implementing the SF execution-status -> twelve-state mapping,
so the two callers can never drift apart on what one status means (§2.3), the
same discipline `console/records_shape.py` set for the `s3-records`
adapter/driver pair (`nousergon-console#98`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ..model.descriptor import Binding
from ..model.entity import Edge, Entity, Provenance
from ..model.kinds import Kind
from ..state_machine_shape import run_state
from ..aws import client as _aws_client
from .base import Cost, DriverResult

name = "state-machine"
kinds = ("run",)
cost = Cost.CHEAP  # one execution-history read, not a fleet-wide scan

#: One execution record as the reader returns it — mirrors the adapter's
#: `ExecutionRecord` shape so a production reader can pass boto3 responses
#: through with minimal mapping, but the driver never imports a vendor SDK.
ExecutionRecord = dict[str, Any]
#: A reader takes (region, state_machine_arn) and returns every execution the
#: source will yield for that machine. Injectable, so tests run over a fixture
#: execution list with no live AWS (groom-sweep §8.1).
ExecutionReader = Callable[[str, str], list[ExecutionRecord]]


def read(binding: Binding, context: dict[str, Any]) -> DriverResult:
    arn = binding.spec.get("state_machine_arn")
    if not arn:
        return DriverResult.failed(
            binding,
            "state-machine requires a `state_machine_arn` — say which "
            "execution history",
            unavailable=("state_machine_arn",),
        )
    region = binding.spec.get("region")
    if not region:
        return DriverResult.failed(
            binding, "state-machine requires a `region`",
            unavailable=("region",),
        )

    reader: ExecutionReader | None = context.get("execution_reader")
    if reader is None:
        reader = _default_reader()
    if reader is None:
        return DriverResult.failed(
            binding,
            "no state-machine reader available (install the `aws` extra, or "
            "inject one) — the binding is declared and unreadable, which is "
            "a different finding from the history being empty",
            unavailable=("reader",),
        )

    try:
        records = reader(str(region), str(arn))
    except Exception as exc:  # noqa: BLE001 - a state, never an exception (§2.3)
        return DriverResult.failed(binding, f"{type(exc).__name__}: {exc}")

    entities: list[Entity] = []
    partial = False
    for rec in records:
        entity = _to_run(rec, str(arn), str(region), binding)
        if entity is None:
            partial = True
            continue
        entities.append(entity)

    cadence = _cadence_seconds(binding.spec)
    return DriverResult(
        binding=binding, entities=tuple(entities), edges=(),
        cadence_seconds=cadence,
        unavailable=("record",) if partial else (),
    )


def _to_run(rec: dict[str, Any], arn: str, region: str, binding: Binding) -> Entity | None:
    execution_arn = str(rec.get("executionArn") or rec.get("arn") or "")
    execution_name = str(rec.get("name") or "")
    run_id = execution_arn or execution_name
    if not run_id:
        return None

    status = str(rec.get("status") or "").upper()
    start = _as_of(rec.get("startDate"))
    stop = _as_of(rec.get("stopDate"))

    return Entity(
        kind=Kind.RUN,
        id=run_id,  # source-assigned (§2.1) — the execution ARN
        state=run_state(status),
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
            "declared_by": binding.component_id,
        },
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


def _default_reader() -> ExecutionReader | None:
    """boto3-backed reader when the optional AWS extra is installed.

    Pages ``list_executions`` fully and hydrates each summary with
    ``describe_execution`` — mirrors the adapter's own default reader.
    Returns None when boto3 is absent so the driver fails loud rather than
    silently returning zero runs.
    """
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ImportError:
        return None

    def reader(region: str, arn: str) -> list[ExecutionRecord]:
        client = _aws_client("stepfunctions", region)
        records: list[ExecutionRecord] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"stateMachineArn": arn, "maxResults": 1000}
            if token:
                kwargs["nextToken"] = token
            page = client.list_executions(**kwargs)
            for summary in page.get("executions") or []:
                execution_arn = summary.get("executionArn")
                rec: ExecutionRecord = dict(summary)
                if execution_arn:
                    try:
                        detail = client.describe_execution(executionArn=execution_arn)
                        rec.update(detail)
                    except (BotoCoreError, ClientError):
                        # Keep the summary; input/output simply unavailable.
                        pass
                records.append(rec)
            token = page.get("nextToken")
            if not token:
                break
        return records

    return reader


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
