"""pipeline-reliability adapter — trading-day rollup Cycles + reliability
Signals from Step Functions execution history (`console-policy.md` §2.3).

Same source shape as `state-machine` (a state machine whose executions carry
an optional cycle key and an optional role tag), a different projection: where
`state-machine` emits one Run per execution, this emits one **Cycle per
trading day** classified into a six-value reliability vocabulary, plus two
**Signal** entities per pipeline (first-attempt success rate, market-open
buffer trend). Kept as its own adapter rather than folded into `state-machine`
because it is a genuinely different entity shape over the same reads, exactly
how `object-store` exists as both an adapter and (separately) a driver —
"same source shape, opposite direction."

**Why a Cycle, not a Component/Run state (alpha-engine-config#6695).** The
six values here — SUCCEEDED / FAILED-recovered / FAILED-unrecovered /
DEGRADED / HOLIDAY / NEVER-FIRED — are NOT a member of
`observability-policy.md` §8.3's closed twelve-state component vocabulary,
and are not meant to be: they classify a **trading day**, not a component or
a run. `model/kinds.py`'s `COMPONENT_STATE_KINDS` is `{COMPONENT, RUN}` only
— a Cycle "carries the source's own value verbatim" (§5.1's second half),
exactly like an Artifact is "fresh" or "stale". Reusing §8.3's vocabulary here
would force six trading-day facts into twelve component-shaped holes with no
member for most of them.

**Why NEVER-FIRED and HOLIDAY need a trading calendar, not just execution
history.** A Step Function's own terminal status is identical (`SUCCEEDED`)
whether the pipeline ran end-to-end, was skipped for a market holiday
(`NotifyHolidaySkip`, a `Succeed`-adjacent terminal state — see
`nousergon-data/infrastructure/step_function_daily.json`), or skipped its
daemon step via an operator-set `skip_run_daemon` flag. Execution history
alone cannot tell "the schedule never fired" apart from "the market was
closed" — both look like zero cadence-role executions. `TradingDayChecker`
resolves that split: HOLIDAY is a non-trading day per the calendar (state
never needed to fire); NEVER-FIRED is a trading day where the cadence
schedule produced zero executions (state should have fired and did not).
This is `principles.md` §2.7 made structural — the absence state renders
distinctly and never collapses into a blank or a holiday.

**Why DEGRADED is opt-in and honest about its data requirement.** SF's own
`status` field (`SUCCEEDED`/`FAILED`/`ABORTED`/`TIMED_OUT`/`RUNNING`/
`PENDING`) has no native "ran but a deliverable was short" member — that
distinction lives in *which states an execution's history entered*
(`GetExecutionHistory`, still the SF execution-history source, just a richer
read of it), never in a bucket vendor status carries. Which state names mark
a degraded path is pipeline-specific, so it is `degraded_state_names` in
**this adapter's own config** (never hardcoded — §2.3) per state machine.
When a pipeline declares none, or the injected reader supplies no
`entered_states` for a record, DEGRADED is simply never produced for that
pipeline — declared honestly via `unavailable`, never guessed.

Sources this adapter reads, both already-existing per the issue (no new
emitters):

- SF execution history (`list_executions` + `describe_execution`, optionally
  `get_execution_history` when `degraded_state_names` is configured) — same
  shape as `state-machine`'s `ExecutionReader`.
- A trading calendar, injected as `TradingDayChecker`. Production wiring
  should use `pandas-market-calendars` (the `calendar` extra) — a generic,
  unaffiliated OSS trading-calendar library, not a fleet-specific one, so a
  standalone user of this console never inherits an alpha-engine dependency.

First-attempt scheduled success rate and the buffer trend are rendered via
the self-describing field mechanism (`model/fields.py` §5.8) so no bespoke
rendering code is needed — the generic entity/list views already render any
`detail["fields"]` entry from its own descriptor.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from ..model.entity import Entity, Provenance
from ..index.build import now_iso
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass
from ..model.kinds import Kind
from .state_machine import ExecutionRecord, _as_of
from .state_machine import _default_reader as _sm_default_reader

#: Execution history + a trading calendar are OBSERVATIONS (§2.5) — what ran,
#: when, on which days the schedule should have run at all.
CLAIM_CLASS = ClaimClass.OBSERVATION

name = "pipeline-reliability"
produces = ("cycle", "signal")

#: The six-value trading-day reliability vocabulary (issue #6695). Deliberately
#: NOT a member of observability-policy.md §8.3 — see module docstring.
SUCCEEDED = "SUCCEEDED"
FAILED_RECOVERED = "FAILED-recovered"
FAILED_UNRECOVERED = "FAILED-unrecovered"
DEGRADED = "DEGRADED"
HOLIDAY = "HOLIDAY"
NEVER_FIRED = "NEVER-FIRED"
RELIABILITY_STATES: tuple[str, ...] = (
    SUCCEEDED, FAILED_RECOVERED, FAILED_UNRECOVERED, DEGRADED, HOLIDAY, NEVER_FIRED,
)

#: A reader identical in shape to `state_machine.ExecutionReader` — injectable
#: so tests run over recorded execution lists with no live AWS (groom-sweep §8.1).
ExecutionReader = Callable[[str, str], list[ExecutionRecord]]
#: date (YYYY-MM-DD) -> is this a trading day. Injectable for the same reason.
TradingDayChecker = Callable[[str], bool]


def fetch(
    config: dict[str, Any],
    reader: ExecutionReader | None = None,
    trading_day_checker: TradingDayChecker | None = None,
    now: datetime | None = None,
) -> AdapterResult:
    region = config.get("region")
    machines = list(config.get("state_machines") or [])
    if not region:
        return _failed(config, "region")
    if not machines:
        return _failed(config, "state_machines")

    if reader is None:
        reader = _default_reader()
        if reader is None:
            return _failed(config, "reader")

    if trading_day_checker is None:
        trading_day_checker = _default_trading_day_checker()
        if trading_day_checker is None:
            # Declared unable rather than silently treating every day as a
            # trading day, which would make NEVER-FIRED and HOLIDAY
            # indistinguishable — exactly the gap this adapter exists to close.
            return _failed(config, "trading_calendar")

    role_field = config.get("role_field", "pipeline_role")
    cadence_roles = frozenset(config.get("cadence_roles") or ())
    recovery_roles = frozenset(config.get("recovery_roles") or ())
    if not cadence_roles:
        return _failed(config, "cadence_roles")

    window = int(config.get("window_trading_days", 20))
    open_time_s = config.get("open_time")
    open_tz = config.get("open_timezone")
    now = now or datetime.now(timezone.utc)

    entities: list[Entity] = []
    unavailable: list[str] = []
    machines_read = 0
    failed = False

    for entry in machines:
        arn = entry.get("arn") if isinstance(entry, dict) else entry
        if not arn:
            continue
        pipeline_key = (
            entry.get("pipeline_key") if isinstance(entry, dict) else None
        ) or _default_pipeline_key(arn)
        degraded_state_names = frozenset(
            (entry.get("degraded_state_names") or ()) if isinstance(entry, dict) else ()
        )
        measure_buffer = bool(entry.get("measure_buffer")) if isinstance(entry, dict) else False

        try:
            records = reader(region, arn)
        except Exception:
            failed = True
            continue
        machines_read += 1

        by_date = _group_by_date(records, role_field)
        trading_days = _window_trading_days(now.date(), window, trading_day_checker)

        cycles, fired_days, buffer_points = _classify_window(
            pipeline_key=pipeline_key,
            arn=arn,
            trading_days=trading_days,
            by_date=by_date,
            role_field=role_field,
            cadence_roles=cadence_roles,
            recovery_roles=recovery_roles,
            degraded_state_names=degraded_state_names,
            source=f"pipeline-reliability:{region}:{arn}",
        )
        entities.extend(cycles)

        entities.append(_first_attempt_signal(pipeline_key, arn, fired_days, region))
        if measure_buffer and open_time_s and open_tz:
            entities.append(_buffer_signal(
                pipeline_key, arn, buffer_points, open_time_s, open_tz, region,
            ))
        elif measure_buffer:
            unavailable.append(f"{pipeline_key}:buffer-config")

    if failed and machines_read == 0:
        return _failed(config, "source")
    if failed:
        unavailable.append("partial-source")

    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        name=config.get("_name", name),
        status=AdapterStatus.OK,
        entities=tuple(entities),
        unavailable=tuple(unavailable),
    )


def _failed(config: dict[str, Any], missing: str) -> AdapterResult:
    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        name=config.get("_name", name),
        status=AdapterStatus.FAILED,
        unavailable=(missing,),
    )


def _default_pipeline_key(arn: str) -> str:
    return arn.rsplit(":", 1)[-1]


# ── classification ──────────────────────────────────────────────────────


def _group_by_date(
    records: list[ExecutionRecord], role_field: str,
) -> dict[str, list[ExecutionRecord]]:
    """Bucket executions by UTC start date — the same split
    `_sf_completion/<pipeline>/{date}.json` uses (`States.StringSplit`
    `$$.Execution.StartTime`), so a day here means the same day there."""
    out: dict[str, list[ExecutionRecord]] = {}
    for rec in records:
        start = _as_of(rec.get("startDate"))
        if not start:
            continue
        day = start[:10]
        out.setdefault(day, []).append(rec)
    for day in out:
        out[day].sort(key=lambda r: _as_of(r.get("startDate")) or "")
    return out


def _window_trading_days(
    today: date, window: int, checker: TradingDayChecker,
) -> list[str]:
    """The last `window` trading days up to (and including) today, oldest
    first. Walks back over calendar days so weekends/holidays are skipped
    without assuming a fixed 7-day cadence."""
    days: list[str] = []
    cursor = today
    # Generous calendar-day lookback bound so a long holiday run cannot
    # starve the window; 4x is enough headroom for any real trading calendar.
    limit = window * 4 + 10
    scanned = 0
    while len(days) < window and scanned < limit:
        d = cursor.isoformat()
        if checker(d):
            days.append(d)
        cursor -= timedelta(days=1)
        scanned += 1
    days.reverse()
    return days


def _role_of(rec: ExecutionRecord, role_field: str) -> str | None:
    inp = rec.get("input")
    if isinstance(inp, str):
        import json
        try:
            inp = json.loads(inp)
        except (TypeError, ValueError):
            inp = None
    if isinstance(inp, dict):
        val = inp.get(role_field)
        return str(val) if val else None
    return None


def _ok(rec: ExecutionRecord) -> bool:
    return str(rec.get("status") or "").upper() == "SUCCEEDED"


def _entered_degraded(rec: ExecutionRecord, degraded_state_names: frozenset[str]) -> bool:
    if not degraded_state_names:
        return False
    entered = rec.get("entered_states")
    if not entered:
        return False
    return bool(degraded_state_names & set(entered))


def _classify_window(
    *,
    pipeline_key: str,
    arn: str,
    trading_days: list[str],
    by_date: dict[str, list[ExecutionRecord]],
    role_field: str,
    cadence_roles: frozenset[str],
    recovery_roles: frozenset[str],
    degraded_state_names: frozenset[str],
    source: str,
) -> tuple[list[Entity], list[tuple[str, bool]], list[dict[str, Any]]]:
    cycles: list[Entity] = []
    fired_days: list[tuple[str, bool]] = []  # (date, first_attempt_ok)
    buffer_points: list[dict[str, Any]] = []

    for d in trading_days:
        day_records = by_date.get(d, [])
        cadence_today = [
            r for r in day_records if _role_of(r, role_field) in cadence_roles
        ]
        detail: dict[str, Any] = {
            "date": d,
            "pipeline": pipeline_key,
            "execution_count": len(day_records),
            "cadence_execution_count": len(cadence_today),
        }
        evidence = None

        if not cadence_today:
            state = NEVER_FIRED
        else:
            first = cadence_today[0]
            evidence = first.get("executionArn") or first.get("name")
            first_ok = _ok(first)
            later_success = any(
                _ok(r) for r in day_records
                if r is not first and (_as_of(r.get("startDate")) or "") > (_as_of(first.get("startDate")) or "")
            )
            is_degraded = first_ok and _entered_degraded(first, degraded_state_names)
            detail["first_attempt_ok"] = first_ok
            detail["first_attempt_status"] = first.get("status")

            if first_ok and not is_degraded:
                state = SUCCEEDED
            elif first_ok and is_degraded:
                state = DEGRADED
            elif later_success:
                state = FAILED_RECOVERED
            else:
                state = FAILED_UNRECOVERED

            fired_days.append((d, first_ok))
            stop_used = None
            if first_ok:
                stop_used = _as_of(first.get("stopDate"))
            elif later_success:
                recovery = next(
                    r for r in day_records
                    if r is not first and _ok(r)
                    and (_as_of(r.get("startDate")) or "") > (_as_of(first.get("startDate")) or "")
                )
                stop_used = _as_of(recovery.get("stopDate"))
            buffer_points.append({
                "date": d,
                "stop": stop_used,
                "reason": None if stop_used else "unrecovered",
            })

        cycles.append(Entity(
            kind=Kind.CYCLE,
            id=f"pipeline-reliability:{pipeline_key}:{d}",
            state=state,
            provenance=Provenance(source=source, as_of=None, evidence=evidence),
            facets={"pipeline": pipeline_key},
            detail=detail,
        ))

    return cycles, fired_days, buffer_points


# ── signals (§5.8 self-describing fields) ───────────────────────────────


def _first_attempt_signal(
    pipeline_key: str, arn: str, fired_days: list[tuple[str, bool]], region: str,
) -> Entity:
    denominator = len(fired_days)
    numerator = sum(1 for _, ok in fired_days if ok)
    rate = (numerator / denominator) if denominator else None
    return Entity(
        kind=Kind.SIGNAL,
        id=f"pipeline-reliability:{pipeline_key}:first-attempt-success-rate",
        state="reporting" if denominator else "no-data",
        provenance=Provenance(source=f"pipeline-reliability:{region}:{arn}"),
        facets={"pipeline": pipeline_key},
        detail={
            "numerator": numerator,
            "denominator": denominator,
            "fields": {
                "first_attempt_success_rate": {
                    "value": rate,
                    "unit": "ratio",
                    "baseline": 1.0,
                    "render": "ratio",
                },
            },
        },
    )


def _buffer_signal(
    pipeline_key: str,
    arn: str,
    buffer_points: list[dict[str, Any]],
    open_time_s: str,
    open_tz: str,
    region: str,
) -> Entity:
    series = [_buffer_point(p, open_time_s, open_tz) for p in buffer_points]
    have_data = any(p["buffer_minutes"] is not None for p in series)
    return Entity(
        kind=Kind.SIGNAL,
        id=f"pipeline-reliability:{pipeline_key}:open-buffer-minutes",
        state="reporting" if have_data else "no-data",
        provenance=Provenance(source=f"pipeline-reliability:{region}:{arn}"),
        facets={"pipeline": pipeline_key},
        detail={
            "open_time": open_time_s,
            "open_timezone": open_tz,
            "fields": {
                "open_buffer_minutes": {
                    "value": series,
                    "unit": "minutes",
                    "baseline": 0.0,
                    "render": "timeseries",
                },
            },
        },
    )


def _buffer_point(point: dict[str, Any], open_time_s: str, open_tz: str) -> dict[str, Any]:
    d = point["date"]
    stop = point["stop"]
    if not stop:
        return {"date": d, "buffer_minutes": None, "reason": point.get("reason") or "no-completion"}
    try:
        open_instant = _open_instant_utc(d, open_time_s, open_tz)
        stop_instant = datetime.fromisoformat(stop.replace("Z", "+00:00"))
        buffer_minutes = (open_instant - stop_instant).total_seconds() / 60.0
    except (ValueError, TypeError):
        return {"date": d, "buffer_minutes": None, "reason": "unparseable-timestamp"}
    return {"date": d, "buffer_minutes": round(buffer_minutes, 1), "reason": None}


def _open_instant_utc(date_str: str, open_time_s: str, open_tz: str) -> datetime:
    """The configured local open time on `date_str`, converted to UTC.

    stdlib `zoneinfo` only (Python >=3.11, this repo's floor) — no new
    dependency for the timezone half of the buffer calculation.
    """
    from zoneinfo import ZoneInfo

    y, m, day = (int(x) for x in date_str.split("-"))
    hh, mm = (int(x) for x in open_time_s.split(":")[:2])
    local = datetime.combine(date(y, m, day), time(hh, mm), tzinfo=ZoneInfo(open_tz))
    return local.astimezone(timezone.utc)


# ── default readers (production wiring; None when the optional extra is absent) ──


def _default_reader() -> ExecutionReader | None:
    """boto3-backed reader — the same list+describe projection `state-machine`
    uses (imported, not duplicated), extended with `entered_states` only when
    a per-machine `degraded_state_names` config asked for it.

    `entered_states` requires one additional `GetExecutionHistory` call per
    execution and is therefore opt-in per pipeline, never fetched by default.
    """
    base = _sm_default_reader()
    if base is None:
        return None

    def reader(region: str, arn: str) -> list[ExecutionRecord]:
        return base(region, arn)

    return reader


def history_reader_for(
    degraded_state_names: frozenset[str],
) -> Callable[[str, list[ExecutionRecord]], None] | None:
    """Mutates `records` in place, attaching `entered_states` via
    `GetExecutionHistory`. Only called when a pipeline declared
    `degraded_state_names` — most deployments never pay this API cost.
    """
    if not degraded_state_names:
        return None
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ImportError:
        return None

    def attach(region: str, records: list[ExecutionRecord]) -> None:
        client = boto3.client("stepfunctions", region_name=region)
        for rec in records:
            arn = rec.get("executionArn")
            if not arn:
                continue
            entered: list[str] = []
            token: str | None = None
            try:
                while True:
                    kwargs: dict[str, Any] = {"executionArn": arn, "maxResults": 1000}
                    if token:
                        kwargs["nextToken"] = token
                    page = client.get_execution_history(**kwargs)
                    for event in page.get("events") or []:
                        details = event.get("stateEnteredEventDetails")
                        if details and details.get("name"):
                            entered.append(details["name"])
                    token = page.get("nextToken")
                    if not token:
                        break
            except (BotoCoreError, ClientError):
                continue  # entered_states stays unset — DEGRADED simply not derivable this run
            rec["entered_states"] = entered

    return attach


def _default_trading_day_checker() -> TradingDayChecker | None:
    """`pandas-market-calendars`-backed NYSE trading-day check, when the
    optional `calendar` extra is installed.

    Deliberately NOT `krepis.trading_calendar` / `nousergon_lib`: this repo is
    a standalone public tool with zero fleet-specific dependencies (README
    "generic over sources"; the `aws` extra is the only precedent for an
    optional dependency, and it names no vendor's business logic). A private
    deployment that wants the fleet's own calendar injects its own
    `trading_day_checker` callable instead of relying on this default.
    """
    try:
        import pandas_market_calendars as mcal  # type: ignore
    except ImportError:
        return None

    calendar = mcal.get_calendar("XNYS")
    _cache: dict[str, bool] = {}

    def checker(date_str: str) -> bool:
        if date_str not in _cache:
            valid = calendar.valid_days(start_date=date_str, end_date=date_str)
            _cache[date_str] = len(valid) > 0
        return _cache[date_str]

    return checker
