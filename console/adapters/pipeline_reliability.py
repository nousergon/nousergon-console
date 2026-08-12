"""pipeline-reliability adapter — trading-day rollup Cycles + reliability
Signals from Step Functions execution history (`console-policy.md` §2.3).

Same source shape as `state-machine` (a state machine whose executions carry
an optional cycle key and an optional role tag), a different projection: where
`state-machine` emits one Run per execution, this emits one **Cycle per cycle
day** classified into a six-value reliability vocabulary, plus Signal entities
per pipeline (first-attempt success rate, attempts-to-success, the rerun
revisit trigger, and the market-open buffer trend). Kept as its own adapter rather than folded into `state-machine`
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

**A cycle day is the pipeline's own cadence, not the market's
(alpha-engine-config-I6919).** `cadence` picks how the window's *expected* days
are enumerated per state machine: `trading-day` (default), `weekday` with
`cadence_weekdays`, or `calendar-day`. A Saturday-scheduled weekly pipeline run
through the NYSE calendar windows none of its own cycle days, and renders every
one HOLIDAY — the absence state collapsing into "not expected", which is the
reading everything below exists to prevent.

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

**Why DEGRADED arrives on two axes, and why neither is vendor status.** SF's
own `status` field (`SUCCEEDED`/`FAILED`/`ABORTED`/`TIMED_OUT`/`RUNNING`/
`PENDING`) has no native "ran but a deliverable was short" member. An
**Option-A** pipeline signals degradation by *failing on purpose* with
`Error: DegradedRun`, so status-keyed watchers engage — `degraded_error_names`
reads that from the execution summary alone. A pipeline that instead signals by
*succeeding* having entered a marker state declares `degraded_state_names`,
which needs a richer read of the same source (`GetExecutionHistory`). Both are
pipeline-specific and live in **this adapter's own config** (never hardcoded —
§2.3). When a pipeline declares neither, DEGRADED is simply never produced for
it — declared honestly via `unavailable`, never guessed.

Deriving it as `first attempt succeeded AND entered a degraded state` — the
rule before alpha-engine-config-I6919 — is a conjunction no Option-A pipeline
can satisfy, so the bucket was unreachable for exactly the pipelines that
degrade honestly, and every one of their degraded runs rendered as an ordinary
red. A pipeline must not get less legible the week it starts telling the truth.

Sources this adapter reads, both already-existing per the issue (no new
emitters):

- SF execution history (`list_executions` + `describe_execution`, optionally
  `get_execution_history` when `degraded_state_names` is configured) — same
  shape as `state-machine`'s `ExecutionReader`.
- A trading calendar, injected as `TradingDayChecker`. Production wiring
  should use `pandas-market-calendars` (the `calendar` extra) — a generic,
  unaffiliated OSS trading-calendar library, not a fleet-specific one, so a
  standalone user of this console never inherits an alpha-engine dependency.

**Attempts-to-success is the headline number, not success rate.** A cycle that
succeeds on rerun 6 and one that succeeds first time both read as "succeeded"
without it, and the difference between them is the entire question the surface
exists to answer. `stage_states` renders how FAR a run got alongside, because
`sf-pipeline-policy.md` §1.1 already orders these outcomes — failing in the
first ten minutes on a preflight is a GOOD outcome, failing at the last stage
after eight hours is a defect — and nothing rendered the ordering.

First-attempt scheduled success rate, attempts-to-success, the rerun trigger
and the buffer trend are all rendered via the self-describing field mechanism (`model/fields.py` §5.8) so no bespoke
rendering code is needed — the generic entity/list views already render any
`detail["fields"]` entry from its own descriptor.

**Structural leaf for `consumed-by` (§3.3/§6, `nousergon-console#52`).** This
adapter's two sources — SF execution history and a trading calendar — carry
nothing about who reads a trading-day Cycle or a reliability Signal once
classified. The genuine consumer of `pipeline-reliability:<pipeline>:...` is
whichever component's registry row declares it under `consumes` (the
`yaml_directory` adapter's existing mechanism) — that is where the fact
belongs, because only the consumer side ever knows what it consumes (§2.3).
Declaring a `consumed-by` edge here would mean inventing a consumer id this
adapter's own reads never supplied, which is exactly the forbidden shape.
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

#: How a pipeline's *expected* cycle days are enumerated. `trading-day` is the
#: weekday-pipeline default and consults the injected calendar; `weekday` names
#: fixed days of the week (a Saturday-scheduled weekly pipeline, which no NYSE
#: calendar will ever return true for); `calendar-day` expects a run every day.
#: alpha-engine-config-I6919: with only `trading-day` available, wiring a weekly
#: pipeline to this adapter renders every one of its cycle days as HOLIDAY — the
#: absence state collapsing into "not expected", which is exactly the reading
#: `principles.md` §2.7 forbids and this adapter exists to prevent.
CADENCE_TRADING_DAY = "trading-day"
CADENCE_CALENDAR_DAY = "calendar-day"
CADENCE_WEEKDAY = "weekday"

#: An execution whose top-level `error` is one of these is DEGRADED whatever its
#: terminal status. This is the Option-A shape (`sf-pipeline-policy.md` §2.3,
#: the 2026-07-28 ruling): a degraded run ends in a `Fail` state carrying
#: `Error: DegradedRun` precisely so status-keyed watchers engage. Before
#: alpha-engine-config-I6919 this adapter derived DEGRADED only from
#: `first_ok and entered a degraded state name` — a conjunction no Option-A
#: pipeline can ever satisfy, because Option-A makes the degraded run FAIL. The
#: DEGRADED bucket was therefore unreachable for exactly the pipelines that
#: implement honest degradation, and every one of their degraded runs rendered
#: as an ordinary red FAILED.
_DEFAULT_DEGRADED_ERRORS: tuple[str, ...] = ("DegradedRun",)

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
        if trading_day_checker is None and any(
            _cadence_rule(m)[0] == CADENCE_TRADING_DAY
            for m in machines
            if isinstance(m, dict) or m
        ):
            # Declared unable rather than silently treating every day as a
            # trading day, which would make NEVER-FIRED and HOLIDAY
            # indistinguishable — exactly the gap this adapter exists to close.
            # Only fatal when some pipeline actually declares a trading-day
            # cadence: a weekly pipeline whose cycle day is a Saturday needs no
            # market calendar, and refusing to render it for want of one the
            # window never consults would be the same false-unavailable in the
            # other direction.
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
        entry = entry if isinstance(entry, dict) else {"arn": arn}
        pipeline_key = entry.get("pipeline_key") or _default_pipeline_key(arn)
        degraded_state_names = frozenset(entry.get("degraded_state_names") or ())
        degraded_errors = frozenset(entry.get("degraded_error_names") or _DEFAULT_DEGRADED_ERRORS)
        measure_buffer = bool(entry.get("measure_buffer"))
        stage_states = tuple(entry.get("stage_states") or ())
        cutovers = {
            str(c.get("date")): c
            for c in (entry.get("cutovers") or ())
            if isinstance(c, dict) and c.get("date")
        }
        noop_max_seconds = entry.get("noop_max_duration_seconds")
        rerun_threshold = int(entry.get("rerun_alert_threshold", 1))

        want_history = bool(degraded_state_names or stage_states)

        try:
            records = reader(region, arn)
            if want_history and not any(r.get("entered_states") for r in records):
                attach = history_reader_for(degraded_state_names | frozenset(stage_states))
                if attach is not None:
                    attach(region, records)
                else:
                    # Declared, never guessed: without history there is no
                    # stage depth and no state-name-derived DEGRADED.
                    unavailable.append(f"{pipeline_key}:execution-history")
        except Exception:
            failed = True
            continue
        machines_read += 1

        by_date = _group_by_date(records, role_field)
        cycle_days = _window_cycle_days(
            now.date(), window, trading_day_checker, _cadence_rule(entry),
        )

        cycles, fired_days, buffer_points, attempt_points = _classify_window(
            pipeline_key=pipeline_key,
            arn=arn,
            trading_days=cycle_days,
            by_date=by_date,
            role_field=role_field,
            cadence_roles=cadence_roles,
            recovery_roles=recovery_roles,
            degraded_state_names=degraded_state_names,
            degraded_errors=degraded_errors,
            stage_states=stage_states,
            cutovers=cutovers,
            noop_max_seconds=noop_max_seconds,
            source=f"pipeline-reliability:{region}:{arn}",
        )
        entities.extend(cycles)

        entities.append(_first_attempt_signal(pipeline_key, arn, fired_days, region))
        entities.append(_attempts_to_success_signal(pipeline_key, arn, attempt_points, region))
        entities.append(_rerun_trigger_signal(
            pipeline_key, arn, attempt_points, rerun_threshold, region,
        ))
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


def _cadence_rule(entry: Any) -> tuple[str, frozenset[int]]:
    """`(cadence, weekdays)` for one state-machine entry.

    `weekdays` is only meaningful for `CADENCE_WEEKDAY` and uses Python's
    `date.weekday()` numbering (Monday 0 .. Sunday 6).
    """
    if not isinstance(entry, dict):
        return CADENCE_TRADING_DAY, frozenset()
    cadence = str(entry.get("cadence") or CADENCE_TRADING_DAY)
    weekdays = frozenset(int(d) for d in (entry.get("cadence_weekdays") or ()))
    return cadence, weekdays


def _window_cycle_days(
    today: date,
    window: int,
    checker: TradingDayChecker | None,
    rule: tuple[str, frozenset[int]],
) -> list[str]:
    """The last `window` *expected cycle days* up to and including today,
    oldest first.

    Walks back over calendar days rather than assuming a fixed stride, so a
    holiday run (trading-day cadence) or a moved schedule (weekday cadence)
    shortens nothing. A cycle day is a day the pipeline's declared cadence says
    a run should exist — which is the denominator every state below is measured
    against, and the reason NEVER-FIRED can be stated at all.
    """
    cadence, weekdays = rule
    if cadence == CADENCE_CALENDAR_DAY:
        expected = lambda d, _c: True  # noqa: E731 - one-line predicate, read in place
    elif cadence == CADENCE_WEEKDAY:
        expected = lambda d, _c: d.weekday() in weekdays  # noqa: E731
    else:
        expected = lambda d, c: bool(c and c(d.isoformat()))  # noqa: E731

    days: list[str] = []
    cursor = today
    # Generous calendar-day lookback bound so a long holiday run — or a weekly
    # cadence, where only one day in seven qualifies — cannot starve the window.
    limit = window * 10 + 10
    scanned = 0
    while len(days) < window and scanned < limit:
        if expected(cursor, checker):
            days.append(cursor.isoformat())
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


def _is_degraded(
    rec: ExecutionRecord,
    degraded_state_names: frozenset[str],
    degraded_errors: frozenset[str],
) -> bool:
    """DEGRADED on EITHER axis, independent of terminal status.

    An Option-A pipeline signals degradation by *failing* with a declared
    error; a pre-Option-A one signals it by succeeding having entered a
    declared state. Both are the same fact and the surface must render it the
    same way, or a pipeline gets less legible the week it starts being honest.
    """
    err = str(rec.get("error") or "")
    if err and err in degraded_errors:
        return True
    return _entered_degraded(rec, degraded_state_names)


def _duration_seconds(rec: ExecutionRecord) -> float | None:
    start, stop = _as_of(rec.get("startDate")), _as_of(rec.get("stopDate"))
    if not start or not stop:
        return None
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(stop.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (b - a).total_seconds()


def _is_noop(rec: ExecutionRecord, noop_max_seconds: float | None) -> bool:
    """A cadence execution that self-gated and terminated immediately.

    The weekly pipeline's trigger fires on more days than it runs and its own
    run-day gate skip-succeeds in seconds. Counting those as cadence successes
    inflates the numerator with runs that did no work — the mirror image of
    counting operator reruns, and just as dishonest. Duration-based rather than
    state-name-based so it needs no execution history; declared per pipeline,
    never assumed (a pipeline that omits it has every execution counted).
    """
    if not noop_max_seconds:
        return False
    if not _ok(rec):
        return False
    dur = _duration_seconds(rec)
    return dur is not None and dur <= float(noop_max_seconds)


def _stage_reached(
    rec: ExecutionRecord, stage_states: tuple[str, ...],
) -> tuple[str | None, int | None]:
    """The deepest declared stage this execution entered, and its 1-based depth.

    `sf-pipeline-policy.md` §1.1 already orders these outcomes — a run failing
    in the first ten minutes on a preflight is a *good* outcome, one failing at
    the last stage after eight hours is a defect — but nothing rendered the
    ordering, so five consecutive reds read as no progress when 13 minutes
    becoming 214 minutes was the fix landing and revealing the next defect.
    """
    if not stage_states:
        return None, None
    entered = rec.get("entered_states")
    if not entered:
        return None, None
    seen = set(entered)
    deepest = None
    for i, stage in enumerate(stage_states, start=1):
        if stage in seen:
            deepest = (stage, i)
    return deepest if deepest else (None, 0)


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
    degraded_errors: frozenset[str],
    stage_states: tuple[str, ...],
    cutovers: dict[str, dict[str, Any]],
    noop_max_seconds: float | None,
    source: str,
) -> tuple[list[Entity], list[tuple[str, bool]], list[dict[str, Any]], list[dict[str, Any]]]:
    cycles: list[Entity] = []
    fired_days: list[tuple[str, bool]] = []  # (date, first_attempt_ok)
    buffer_points: list[dict[str, Any]] = []
    attempt_points: list[dict[str, Any]] = []

    for d in trading_days:
        day_records = by_date.get(d, [])
        cadence_today = [
            r for r in day_records
            if _role_of(r, role_field) in cadence_roles and not _is_noop(r, noop_max_seconds)
        ]
        gate_noops = sum(
            1 for r in day_records
            if _role_of(r, role_field) in cadence_roles and _is_noop(r, noop_max_seconds)
        )
        # Attempts = the scheduled run plus every operator overlay that tried to
        # complete the SAME cycle. Roles outside both sets (the weekly
        # pipeline's chained `exercise` runs, for instance) are counted in
        # execution_count and deliberately excluded here: they are a different
        # question than "did the trigger path work".
        attempts_records = [
            r for r in day_records
            if _role_of(r, role_field) in (cadence_roles | recovery_roles)
            and not _is_noop(r, noop_max_seconds)
        ]
        detail: dict[str, Any] = {
            "date": d,
            "pipeline": pipeline_key,
            "execution_count": len(day_records),
            "cadence_execution_count": len(cadence_today),
            "attempts": len(attempts_records),
            "attempts_to_success": None,
            "gate_noop_count": gate_noops,
        }
        if d in cutovers:
            cut = cutovers[d]
            # A regression whose cause is a known cutover is a different fact
            # from an unexplained one; the correlation cost manual archaeology
            # to see once and should never cost it twice.
            detail["cutover"] = {
                "label": cut.get("label"),
                "ref": cut.get("ref"),
            }
        evidence = None

        # 1-based index of the first attempt that reached a non-degraded
        # success — the headline number. A cycle that succeeds on rerun 6 and
        # one that succeeds first time both read as "succeeded" without it, and
        # the difference between them is the entire question.
        for i, r in enumerate(attempts_records, start=1):
            if _ok(r) and not _is_degraded(r, degraded_state_names, degraded_errors):
                detail["attempts_to_success"] = i
                break

        if not cadence_today:
            state = NEVER_FIRED
        else:
            first = cadence_today[0]
            evidence = first.get("executionArn") or first.get("name")
            first_ok = _ok(first)
            first_degraded = _is_degraded(first, degraded_state_names, degraded_errors)
            later_success = any(
                _ok(r) and not _is_degraded(r, degraded_state_names, degraded_errors)
                for r in day_records
                if r is not first and (_as_of(r.get("startDate")) or "") > (_as_of(first.get("startDate")) or "")
            )
            detail["first_attempt_ok"] = first_ok
            detail["first_attempt_status"] = first.get("status")
            if first.get("error"):
                detail["first_attempt_error"] = first.get("error")
            stage, depth = _stage_reached(first, stage_states)
            if stage_states:
                detail["stage_reached"] = stage
                detail["stage_depth"] = depth
                detail["stage_count"] = len(stage_states)

            if first_degraded:
                # DEGRADED regardless of terminal status — an Option-A run that
                # completed its work and told the truth about it is not the same
                # fact as one that crashed, and must not render as one.
                state = DEGRADED
            elif first_ok:
                state = SUCCEEDED
            elif later_success:
                state = FAILED_RECOVERED
            else:
                state = FAILED_UNRECOVERED

            # First-attempt success counts only a CLEAN first attempt: a
            # degraded run that reports success is precisely the run this
            # metric must not count as one that worked.
            fired_days.append((d, first_ok and not first_degraded))
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
            attempt_points.append({
                "date": d,
                "attempts": len(attempts_records),
                "attempts_to_success": detail["attempts_to_success"],
                "stage_depth": detail.get("stage_depth"),
                "stage_reached": detail.get("stage_reached"),
                "cutover": (detail.get("cutover") or {}).get("label"),
                "state": state,
            })

        cycles.append(Entity(
            kind=Kind.CYCLE,
            id=f"pipeline-reliability:{pipeline_key}:{d}",
            state=state,
            provenance=Provenance(source=source, as_of=None, evidence=evidence),
            facets={"pipeline": pipeline_key},
            detail=detail,
        ))

    return cycles, fired_days, buffer_points, attempt_points


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


def _attempts_to_success_signal(
    pipeline_key: str, arn: str, attempt_points: list[dict[str, Any]], region: str,
) -> Entity:
    """Attempts-to-success — the headline number, not success rate.

    alpha-engine-config-I6919: reconstructing 26 days of this by hand from
    `list-executions` is what it previously took to answer "are we making
    progress". 16 attempts on one cycle falling to first-attempt success on
    four consecutive cycles IS the progress, and success rate renders both as
    "succeeded". The `series` carries stage depth alongside, so a run failing
    LATER than the last one reads as progress rather than as another red.
    """
    solved = [p["attempts_to_success"] for p in attempt_points if p["attempts_to_success"]]
    mean = (sum(solved) / len(solved)) if solved else None
    return Entity(
        kind=Kind.SIGNAL,
        id=f"pipeline-reliability:{pipeline_key}:attempts-to-success",
        state="reporting" if attempt_points else "no-data",
        provenance=Provenance(source=f"pipeline-reliability:{region}:{arn}"),
        facets={"pipeline": pipeline_key},
        detail={
            "cycles": len(attempt_points),
            "cycles_reaching_success": len(solved),
            "series": attempt_points,
            "fields": {
                "mean_attempts_to_success": {
                    "value": (round(mean, 2) if mean is not None else None),
                    "unit": "attempts",
                    "baseline": 1.0,
                    "render": "number",
                },
            },
        },
    )


def _rerun_trigger_signal(
    pipeline_key: str,
    arn: str,
    attempt_points: list[dict[str, Any]],
    threshold: int,
    region: str,
) -> Entity:
    """`sf-pipeline-policy.md` §1's revisit trigger, as a detector.

    *"Any run requiring >1 operator rerun to reach a complete (non-degraded)
    terminal state"* was a declared trigger with no detector — it fired only
    when somebody noticed. It fires off this data now. A cycle that never
    reached success counts as breaching too: zero reruns that worked is not
    evidence the threshold was respected.
    """
    breaches = [
        {
            "date": p["date"],
            "attempts": p["attempts"],
            "attempts_to_success": p["attempts_to_success"],
            "state": p["state"],
        }
        for p in attempt_points
        if p["attempts_to_success"] is None or p["attempts_to_success"] > threshold + 1
    ]
    if not attempt_points:
        state = "no-data"
    else:
        state = "firing" if breaches else "clear"
    return Entity(
        kind=Kind.SIGNAL,
        id=f"pipeline-reliability:{pipeline_key}:rerun-revisit-trigger",
        state=state,
        provenance=Provenance(source=f"pipeline-reliability:{region}:{arn}"),
        facets={"pipeline": pipeline_key},
        detail={
            "policy": "sf-pipeline-policy.md §1 revisit trigger",
            "operator_rerun_threshold": threshold,
            "breaches": breaches,
            "fields": {
                "cycles_over_rerun_threshold": {
                    "value": len(breaches),
                    "unit": "cycles",
                    "baseline": 0,
                    "render": "number",
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
