"""pipeline-reliability adapter tests — recorded execution fixtures, no live
AWS and no live trading calendar (this repo is public, AGPL — no fleet
topology anywhere, tests included).

Covers alpha-engine-config#6695: the six-value trading-day classification
(all six, including NEVER-FIRED vs HOLIDAY distinctness), first-attempt
scheduled success rate with its denominator, and the market-open buffer trend.
"""
from __future__ import annotations

from datetime import datetime, timezone

from console.adapters import pipeline_reliability as pr
from console.model.kinds import Kind

ARN = "arn:aws:states:xx-test-1:000000000000:stateMachine:fixture-preopen"

# A 6-trading-day window: Mon 8/3 .. Mon 8/10 minus the weekend, with 8/6
# reserved as a synthetic market holiday not present in TRADING_DAYS.
TRADING_DAYS = {
    "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-07", "2026-08-10",
}


def _checker(days=TRADING_DAYS):
    return lambda d: d in days


def _exec(name, status, start, stop, role=None, entered=None, execution_date=None):
    rec = {
        "executionArn": f"arn:aws:states:xx-test-1:000000000000:execution:fixture-preopen:{name}",
        "name": name,
        "status": status,
        "startDate": start,
        "stopDate": stop,
        "input": {"pipeline_role": role, "execution_date": execution_date or start[:10]},
    }
    if entered is not None:
        rec["entered_states"] = entered
    return rec


def _reader_for(records):
    def reader(region, arn):
        assert region == "xx-test-1"
        assert arn == ARN
        return list(records)
    return reader


def _cfg(**extra):
    return {
        "region": "xx-test-1",
        "state_machines": [
            {"arn": ARN, "pipeline_key": "preopen", "measure_buffer": True,
             **({"degraded_state_names": extra.pop("degraded_state_names")}
                if "degraded_state_names" in extra else {})},
        ],
        "role_field": "pipeline_role",
        "cadence_roles": ["daily"],
        "recovery_roles": ["watch-rerun", "recovery"],
        "window_trading_days": 5,
        "open_time": "06:30",
        "open_timezone": "America/Los_Angeles",
        **extra,
    }


def _now():
    # Tue 2026-08-11 — one calendar day past the window's last trading day
    # (Mon 2026-08-10), so every day already inside the fixture window is a
    # CLOSED cycle day and renders on its historical outcome, never PENDING.
    # `test_current_trading_day_before_close_renders_pending` below freezes a
    # separate `now` inside the still-open day to cover the I6758 path.
    return datetime(2026, 8, 11, 20, 0, tzinfo=timezone.utc)


def _cycles_by_date(result):
    return {
        e.detail["date"]: e
        for e in result.entities
        if e.kind is Kind.CYCLE and e.facets.get("pipeline") == "preopen"
    }


# ---------------------------------------------------------------- states --


def test_succeeded_day():
    records = [_exec("run-1", "SUCCEEDED", "2026-08-03T13:00:00Z", "2026-08-03T13:12:00Z", role="daily")]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=_now())
    cycles = _cycles_by_date(result)
    assert cycles["2026-08-03"].state == pr.SUCCEEDED


def test_failed_recovered_day():
    records = [
        _exec("run-1", "FAILED", "2026-08-04T13:00:00Z", "2026-08-04T13:05:00Z", role="daily"),
        _exec("run-2", "SUCCEEDED", "2026-08-04T14:00:00Z", "2026-08-04T14:10:00Z", role="watch-rerun"),
    ]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=_now())
    cycles = _cycles_by_date(result)
    assert cycles["2026-08-04"].state == pr.FAILED_RECOVERED


def test_failed_unrecovered_day():
    records = [_exec("run-1", "FAILED", "2026-08-05T13:00:00Z", "2026-08-05T13:05:00Z", role="daily")]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=_now())
    cycles = _cycles_by_date(result)
    assert cycles["2026-08-05"].state == pr.FAILED_UNRECOVERED


def test_degraded_day_needs_entered_states_and_config():
    records = [
        _exec("run-1", "SUCCEEDED", "2026-08-07T13:00:00Z", "2026-08-07T13:12:00Z",
              role="daily", entered=["CheckSkipRunDaemon", "WriteCompletionMarker"]),
    ]
    result = pr.fetch(
        _cfg(degraded_state_names=["CheckSkipRunDaemon"]),
        reader=_reader_for(records), trading_day_checker=_checker(), now=_now(),
    )
    cycles = _cycles_by_date(result)
    assert cycles["2026-08-07"].state == pr.DEGRADED


def test_succeeded_not_degraded_without_configured_degraded_state_names():
    """Same entered_states, but the pipeline never declared them as
    degraded-marking — must never guess DEGRADED (§2.3: configured, not
    inferred from a pattern the adapter invented)."""
    records = [
        _exec("run-1", "SUCCEEDED", "2026-08-07T13:00:00Z", "2026-08-07T13:12:00Z",
              role="daily", entered=["CheckSkipRunDaemon", "WriteCompletionMarker"]),
    ]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=_now())
    cycles = _cycles_by_date(result)
    assert cycles["2026-08-07"].state == pr.SUCCEEDED


def test_holiday_and_never_fired_are_distinct():
    """8/6 is not in TRADING_DAYS (a synthetic holiday) — the calendar decides
    HOLIDAY. 8/10 IS a trading day with zero cadence executions — NEVER-FIRED.
    Neither renders as the other, and neither renders as blank/green."""
    records: list = []  # nothing fires anywhere in the window
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=_now())
    cycles = _cycles_by_date(result)
    assert "2026-08-06" not in cycles  # not a trading day at all — never windowed
    assert cycles["2026-08-10"].state == pr.NEVER_FIRED
    assert cycles["2026-08-10"].state != "HOLIDAY"


def test_current_trading_day_before_close_renders_pending():
    """alpha-engine-config-I6758: 2026-08-10 is Monday and still IN PROGRESS
    (frozen mid-day PT, well before the calendar day closes in the exchange
    timezone) with zero cadence executions. Not evidence the schedule failed —
    it may not have been triggered yet. Distinct from both NEVER-FIRED (a
    closed day, still zero executions) and HOLIDAY (never expected)."""
    mid_day = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)  # 08:00 PDT
    result = pr.fetch(_cfg(), reader=_reader_for([]), trading_day_checker=_checker(), now=mid_day)
    cycles = _cycles_by_date(result)
    assert cycles["2026-08-10"].state == pr.PENDING
    assert cycles["2026-08-10"].state != pr.NEVER_FIRED
    assert cycles["2026-08-10"].state != pr.HOLIDAY


def test_a_cadence_execution_on_the_still_open_day_classifies_normally():
    """A day that already fired is classified on its actual outcome even if
    its calendar day has not closed — PENDING only replaces a bare
    zero-execution NEVER-FIRED, it never masks a real result."""
    mid_day = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)
    records = [_exec("run-1", "SUCCEEDED", "2026-08-10T13:00:00Z", "2026-08-10T13:12:00Z", role="daily")]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=mid_day)
    cycles = _cycles_by_date(result)
    assert cycles["2026-08-10"].state == pr.SUCCEEDED


def test_closed_day_with_zero_executions_is_still_never_fired():
    """The day after the still-open day above: 2026-08-10 is now CLOSED
    (frozen the next calendar day in the exchange timezone) and zero cadence
    executions is a real absence again."""
    result = pr.fetch(_cfg(), reader=_reader_for([]), trading_day_checker=_checker(), now=_now())
    cycles = _cycles_by_date(result)
    assert cycles["2026-08-10"].state == pr.NEVER_FIRED


def test_never_fired_is_zero_cadence_executions_even_if_something_ran():
    """An ad-hoc/adhoc-role execution on a day the schedule never fired is
    still NEVER-FIRED — an operator poking the pipeline is not the cycle."""
    records = [_exec("run-1", "SUCCEEDED", "2026-08-05T13:00:00Z", "2026-08-05T13:05:00Z", role="smoke")]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=_now())
    cycles = _cycles_by_date(result)
    assert cycles["2026-08-05"].state == pr.NEVER_FIRED


def test_window_covers_exactly_the_configured_trading_days():
    result = pr.fetch(_cfg(), reader=_reader_for([]), trading_day_checker=_checker(), now=_now())
    cycles = _cycles_by_date(result)
    assert set(cycles) == {"2026-08-03", "2026-08-04", "2026-08-05", "2026-08-07", "2026-08-10"}


# ============================================================= #
# alpha-engine-config-I7068 — floor/ceiling from execution      #
# history, and the rendered window length stated on the strip.  #
# ============================================================= #


def test_floor_drops_cycle_days_older_than_the_oldest_execution():
    """The state machine's oldest surviving execution is 2026-08-05 — a cycle
    day before that did not fail to fire, it did not exist yet, and must not
    render NEVER-FIRED. Dropped from the strip entirely, not `UNKNOWN`."""
    records = [_exec("run-1", "SUCCEEDED", "2026-08-05T13:00:00Z", "2026-08-05T13:12:00Z", role="daily")]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=_now())
    cycles = _cycles_by_date(result)
    assert "2026-08-03" not in cycles
    assert "2026-08-04" not in cycles
    assert cycles["2026-08-05"].state == pr.SUCCEEDED
    assert set(cycles) == {"2026-08-05", "2026-08-07", "2026-08-10"}


def test_floor_does_not_apply_when_no_execution_evidence_exists_at_all():
    """An empty read is "no evidence", not "evidence of nothing" — applying a
    floor from zero records would drop the entire window, which is a worse
    false reading than the one this closes."""
    result = pr.fetch(_cfg(), reader=_reader_for([]), trading_day_checker=_checker(), now=_now())
    cycles = _cycles_by_date(result)
    assert set(cycles) == {"2026-08-03", "2026-08-04", "2026-08-05", "2026-08-07", "2026-08-10"}


def test_window_coverage_signal_states_requested_versus_rendered_length():
    records = [_exec("run-1", "SUCCEEDED", "2026-08-05T13:00:00Z", "2026-08-05T13:12:00Z", role="daily")]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=_now())
    signal = next(
        e for e in result.entities
        if e.kind is Kind.SIGNAL and e.id.endswith("window-coverage")
        and e.facets.get("pipeline") == "preopen"
    )
    assert signal.detail["floor_date"] == "2026-08-05"
    assert signal.detail["fields"]["window_days_requested"]["value"] == 5
    assert signal.detail["fields"]["window_days_rendered"]["value"] == 3  # 8/5, 8/7, 8/10


def test_window_trading_days_overridable_per_state_machine_entry():
    """Deliverable 3's window half — falls back to the adapter-level default
    when an entry does not declare its own."""
    cfg = _cfg()
    cfg["state_machines"][0]["window_trading_days"] = 2
    result = pr.fetch(cfg, reader=_reader_for([]), trading_day_checker=_checker(), now=_now())
    cycles = _cycles_by_date(result)
    assert set(cycles) == {"2026-08-07", "2026-08-10"}


# ------------------------------------------------------------ first-attempt --


def test_first_attempt_success_rate_and_denominator():
    records = [
        _exec("run-1", "SUCCEEDED", "2026-08-03T13:00:00Z", "2026-08-03T13:12:00Z", role="daily"),
        _exec("run-2", "FAILED", "2026-08-04T13:00:00Z", "2026-08-04T13:05:00Z", role="daily"),
        _exec("run-3", "SUCCEEDED", "2026-08-04T14:00:00Z", "2026-08-04T14:10:00Z", role="watch-rerun"),
        _exec("run-4", "FAILED", "2026-08-05T13:00:00Z", "2026-08-05T13:05:00Z", role="daily"),
        # 8/7 NEVER-FIRED (no cadence run), 8/10 NEVER-FIRED — neither counts
        # toward the denominator: it is SCHEDULED executions only (§ issue).
    ]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=_now())
    signal = next(
        e for e in result.entities
        if e.kind is Kind.SIGNAL and e.id.endswith("first-attempt-success-rate")
    )
    assert signal.detail["denominator"] == 3  # 8/3, 8/4, 8/5 had a cadence execution
    assert signal.detail["numerator"] == 1  # only 8/3's first attempt succeeded
    field = signal.detail["fields"]["first_attempt_success_rate"]
    assert abs(field["value"] - (1 / 3)) < 1e-9
    assert field["baseline"] == 1.0
    assert field["render"] == "ratio"


def test_first_attempt_rate_renders_no_data_when_nothing_fired():
    result = pr.fetch(_cfg(), reader=_reader_for([]), trading_day_checker=_checker(), now=_now())
    signal = next(
        e for e in result.entities
        if e.kind is Kind.SIGNAL and e.id.endswith("first-attempt-success-rate")
    )
    assert signal.detail["denominator"] == 0
    assert signal.state == "no-data"
    assert signal.detail["fields"]["first_attempt_success_rate"]["value"] is None


# -------------------------------------------------------------------- buffer --


def test_buffer_trend_positive_when_finished_before_open():
    # 06:30 America/Los_Angeles on 2026-08-03 is 13:30 UTC (PDT, UTC-7).
    records = [_exec("run-1", "SUCCEEDED", "2026-08-03T12:00:00Z", "2026-08-03T13:00:00Z", role="daily")]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=_now())
    signal = next(
        e for e in result.entities
        if e.kind is Kind.SIGNAL and e.id.endswith("open-buffer-minutes")
    )
    series = signal.detail["fields"]["open_buffer_minutes"]["value"]
    point = next(p for p in series if p["date"] == "2026-08-03")
    assert point["buffer_minutes"] == 30.0  # finished 13:00 UTC, open at 13:30 UTC


def test_buffer_trend_negative_when_finished_after_open():
    records = [_exec("run-1", "SUCCEEDED", "2026-08-03T12:00:00Z", "2026-08-03T14:00:00Z", role="daily")]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=_now())
    signal = next(
        e for e in result.entities
        if e.kind is Kind.SIGNAL and e.id.endswith("open-buffer-minutes")
    )
    series = signal.detail["fields"]["open_buffer_minutes"]["value"]
    point = next(p for p in series if p["date"] == "2026-08-03")
    assert point["buffer_minutes"] == -30.0


def test_buffer_trend_uses_recovery_completion_when_first_attempt_failed():
    records = [
        _exec("run-1", "FAILED", "2026-08-04T12:00:00Z", "2026-08-04T12:30:00Z", role="daily"),
        _exec("run-2", "SUCCEEDED", "2026-08-04T13:00:00Z", "2026-08-04T13:15:00Z", role="watch-rerun"),
    ]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=_now())
    signal = next(
        e for e in result.entities
        if e.kind is Kind.SIGNAL and e.id.endswith("open-buffer-minutes")
    )
    series = signal.detail["fields"]["open_buffer_minutes"]["value"]
    point = next(p for p in series if p["date"] == "2026-08-04")
    assert point["buffer_minutes"] == 15.0  # open 13:30 UTC minus recovery stop 13:15 UTC


def test_buffer_trend_declares_absence_not_zero_when_unrecovered():
    records = [_exec("run-1", "FAILED", "2026-08-05T12:00:00Z", "2026-08-05T12:30:00Z", role="daily")]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=_now())
    signal = next(
        e for e in result.entities
        if e.kind is Kind.SIGNAL and e.id.endswith("open-buffer-minutes")
    )
    series = signal.detail["fields"]["open_buffer_minutes"]["value"]
    point = next(p for p in series if p["date"] == "2026-08-05")
    assert point["buffer_minutes"] is None
    assert point["reason"] == "unrecovered"


def test_buffer_not_emitted_when_open_time_missing_and_measure_buffer_set():
    cfg = _cfg()
    cfg.pop("open_time")
    result = pr.fetch(cfg, reader=_reader_for([]), trading_day_checker=_checker(), now=_now())
    assert not any(e.id.endswith("open-buffer-minutes") for e in result.entities)
    assert "preopen:buffer-config" in result.unavailable


# --------------------------------------------------------------- honesty --


def test_no_reader_and_no_boto3_fails_loud_not_empty(monkeypatch):
    # With no injectable reader the adapter falls back to the boto3 default
    # (`pr._sm_default_reader`, re-exported from `state_machine._default_reader`).
    # Force the "boto3 absent" path so the test is hermetic on every host —
    # boto3 IS installed as a fleet-wide pin on this machine (measured
    # 2026-08-10: `import boto3` succeeds), so asserting on ambient package
    # absence is what made this test flip from FAILED("reader") to
    # FAILED("source") here: a real `_default_reader()` was returned, it was
    # called, and the network call raised with no AWS credentials configured.
    # `tests/test_state_machine.py::test_no_reader_is_failed_not_empty` uses
    # the same monkeypatch shape one layer down.
    monkeypatch.setattr(pr, "_sm_default_reader", lambda: None)
    result = pr.fetch(_cfg(), reader=None, trading_day_checker=_checker(), now=_now())
    from console.model.envelope import AdapterStatus
    assert result.status is AdapterStatus.FAILED
    assert "reader" in result.unavailable


def test_missing_trading_calendar_fails_loud_not_silent():
    result = pr.fetch(_cfg(), reader=_reader_for([]), trading_day_checker=None, now=_now())
    from console.model.envelope import AdapterStatus
    assert result.status is AdapterStatus.FAILED
    assert "trading_calendar" in result.unavailable


def test_missing_cadence_roles_fails_loud():
    cfg = _cfg()
    cfg["cadence_roles"] = []
    result = pr.fetch(cfg, reader=_reader_for([]), trading_day_checker=_checker(), now=_now())
    from console.model.envelope import AdapterStatus
    assert result.status is AdapterStatus.FAILED
    assert "cadence_roles" in result.unavailable


def test_pipeline_reliability_is_registered():
    from console.config import ADAPTERS
    assert ADAPTERS["pipeline-reliability"] is pr


def test_no_consumed_by_edges_declared_structural_leaf():
    """nousergon-console#52: pipeline-reliability is a documented structural
    leaf for `consumed-by`. Its sources (SF execution history + a trading
    calendar) carry nothing about who reads a classified Cycle or Signal —
    that fact belongs to whichever component's registry row declares it
    under `consumes` (yaml_directory's existing mechanism), because only the
    consumer side ever knows what it consumes (§2.3). This is the explicit
    "no consumer relation" record so a future audit does not re-flag it."""
    records = [_exec("run-1", "SUCCEEDED", "2026-08-03T13:00:00Z", "2026-08-03T13:12:00Z", role="daily")]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=_now())
    assert result.entities  # sanity: the fixture does produce entities
    assert result.edges == ()


def test_cycle_state_is_not_a_component_state_vocabulary_member():
    """§5.1's second half: Cycle is outside COMPONENT_STATE_KINDS, so the
    seven-value string is legal without touching the closed thirteen."""
    from console.model.kinds import COMPONENT_STATE_KINDS, Kind
    assert Kind.CYCLE not in COMPONENT_STATE_KINDS


def test_reliability_vocabulary_is_closed_at_seven_values():
    """PENDING (alpha-engine-config-I6758) is a deliberate seventh member, not
    a reuse of HOLIDAY or NEVER-FIRED — the vocabulary stays closed and this
    pins its size so a future addition is a deliberate edit, not a drift."""
    assert set(pr.RELIABILITY_STATES) == {
        pr.SUCCEEDED, pr.FAILED_RECOVERED, pr.FAILED_UNRECOVERED,
        pr.DEGRADED, pr.HOLIDAY, pr.NEVER_FIRED, pr.PENDING,
    }
    assert len(pr.RELIABILITY_STATES) == 7


# ===================================================================== #
# alpha-engine-config-I6919 — weekly cadence, attempts-to-success,      #
# stage depth, Option-A DEGRADED, cutover markers, the rerun trigger.   #
# ===================================================================== #

WEEKLY_ARN = "arn:aws:states:xx-test-1:000000000000:stateMachine:fixture-weekly"


def _wexec(name, status, start, stop, role=None, entered=None, error=None):
    rec = {
        "executionArn": f"arn:aws:states:xx-test-1:000000000000:execution:fixture-weekly:{name}",
        "name": name,
        "status": status,
        "startDate": start,
        "stopDate": stop,
        "input": {"pipeline_role": role},
    }
    if entered is not None:
        rec["entered_states"] = entered
    if error is not None:
        rec["error"] = error
    return rec


def _weekly_cfg(**machine_extra):
    return {
        "region": "xx-test-1",
        "state_machines": [{
            "arn": WEEKLY_ARN,
            "pipeline_key": "weekly",
            "cadence": pr.CADENCE_WEEKDAY,
            "cadence_weekdays": [5],  # Saturday — no market calendar will ever say yes
            **machine_extra,
        }],
        "role_field": "pipeline_role",
        "cadence_roles": ["weekly"],
        "recovery_roles": ["watch-rerun", "recovery"],
        "window_trading_days": 3,
    }


def _weekly_reader(records):
    def reader(region, arn):
        assert arn == WEEKLY_ARN
        return list(records)
    return reader


def _wcycles(result):
    return {
        e.detail["date"]: e
        for e in result.entities
        if e.kind is Kind.CYCLE and e.facets.get("pipeline") == "weekly"
    }


def _signal(result, suffix, pipeline="weekly"):
    return next(
        e for e in result.entities
        if e.kind is Kind.SIGNAL and e.id.endswith(suffix)
        and e.facets.get("pipeline") == pipeline
    )


# ------------------------------------------------------------- cadence days --


def test_weekly_cadence_windows_saturdays_not_trading_days():
    """The defect this closes: a Saturday-scheduled pipeline run through the
    NYSE trading-day checker windows ZERO of its own cycle days, so every
    weekly cycle renders HOLIDAY — the absence state collapsing into "not
    expected", which is exactly what NEVER-FIRED exists to prevent."""
    result = pr.fetch(
        _weekly_cfg(),
        reader=_weekly_reader([]),
        trading_day_checker=lambda d: False,  # a market calendar: never a Saturday
        now=_now(),
    )
    # _now() is Tue 2026-08-11; the last three Saturdays are 8/8, 8/1, 7/25.
    assert set(_wcycles(result)) == {"2026-07-25", "2026-08-01", "2026-08-08"}
    assert all(c.state == pr.NEVER_FIRED for c in _wcycles(result).values())


def test_calendar_day_cadence_expects_every_day():
    cfg = _weekly_cfg()
    cfg["state_machines"][0]["cadence"] = pr.CADENCE_CALENDAR_DAY
    result = pr.fetch(cfg, reader=_weekly_reader([]), trading_day_checker=lambda d: False, now=_now())
    assert set(_wcycles(result)) == {"2026-08-09", "2026-08-10", "2026-08-11"}


def test_missing_trading_calendar_is_not_fatal_for_a_non_trading_day_cadence():
    """A weekly pipeline needs no market calendar. Refusing to render it for
    want of one its window never consults is a false-unavailable."""
    result = pr.fetch(_weekly_cfg(), reader=_weekly_reader([]), trading_day_checker=None, now=_now())
    # No calendar injected and none installed in the test env — still OK,
    # because no configured pipeline declared a trading-day cadence.
    assert result.status.value != "failed" or "trading_calendar" not in result.unavailable


# ------------------------------------------------------- Option-A DEGRADED --


def test_option_a_degraded_run_renders_degraded_not_failed():
    """A degraded run under the Option-A ruling ends in a Fail state carrying
    `Error: DegradedRun`. Deriving DEGRADED from `first_ok AND entered a
    degraded state` is a conjunction no such run can satisfy, so the bucket was
    unreachable for exactly the pipelines that degrade honestly."""
    records = [_wexec("run-1", "FAILED", "2026-08-08T09:00:00Z", "2026-08-08T11:00:00Z",
                      role="weekly", error="DegradedRun")]
    result = pr.fetch(_weekly_cfg(), reader=_weekly_reader(records),
                      trading_day_checker=lambda d: False, now=_now())
    assert _wcycles(result)["2026-08-08"].state == pr.DEGRADED


def test_a_genuine_crash_is_still_failed():
    records = [_wexec("run-1", "FAILED", "2026-08-08T09:00:00Z", "2026-08-08T09:03:00Z",
                      role="weekly", error="States.TaskFailed")]
    result = pr.fetch(_weekly_cfg(), reader=_weekly_reader(records),
                      trading_day_checker=lambda d: False, now=_now())
    assert _wcycles(result)["2026-08-08"].state == pr.FAILED_UNRECOVERED


def test_degraded_first_attempt_does_not_count_as_first_attempt_success():
    records = [_wexec("run-1", "FAILED", "2026-08-08T09:00:00Z", "2026-08-08T11:00:00Z",
                      role="weekly", error="DegradedRun")]
    result = pr.fetch(_weekly_cfg(), reader=_weekly_reader(records),
                      trading_day_checker=lambda d: False, now=_now())
    assert _signal(result, "first-attempt-success-rate").detail["numerator"] == 0


# ------------------------------------------------------ attempts-to-success --


def test_attempts_to_success_counts_the_rerun_that_worked():
    records = [
        _wexec("run-1", "FAILED", "2026-08-08T09:00:00Z", "2026-08-08T09:13:00Z", role="weekly"),
        _wexec("rr-1", "FAILED", "2026-08-08T10:00:00Z", "2026-08-08T10:20:00Z", role="watch-rerun"),
        _wexec("rr-2", "SUCCEEDED", "2026-08-08T11:00:00Z", "2026-08-08T14:00:00Z", role="watch-rerun"),
    ]
    result = pr.fetch(_weekly_cfg(), reader=_weekly_reader(records),
                      trading_day_checker=lambda d: False, now=_now())
    cyc = _wcycles(result)["2026-08-08"]
    assert cyc.detail["attempts"] == 3
    assert cyc.detail["attempts_to_success"] == 3
    assert cyc.state == pr.FAILED_RECOVERED
    sig = _signal(result, "attempts-to-success")
    assert sig.detail["fields"]["mean_attempts_to_success"]["value"] == 3.0
    assert sig.detail["fields"]["mean_attempts_to_success"]["baseline"] == 1.0


def test_attempts_to_success_is_none_when_no_attempt_succeeded():
    records = [
        _wexec("run-1", "FAILED", "2026-08-08T09:00:00Z", "2026-08-08T09:13:00Z", role="weekly"),
        _wexec("rr-1", "FAILED", "2026-08-08T10:00:00Z", "2026-08-08T10:20:00Z", role="watch-rerun"),
    ]
    result = pr.fetch(_weekly_cfg(), reader=_weekly_reader(records),
                      trading_day_checker=lambda d: False, now=_now())
    assert _wcycles(result)["2026-08-08"].detail["attempts_to_success"] is None


def test_roles_outside_cadence_and_recovery_are_not_attempts():
    """The weekly pipeline's chained `exercise` runs are a different question
    than "did the trigger path work" — counted in execution_count, never in
    attempts."""
    records = [
        _wexec("run-1", "SUCCEEDED", "2026-08-08T09:00:00Z", "2026-08-08T12:00:00Z", role="weekly"),
        _wexec("ex-1", "FAILED", "2026-08-08T22:00:00Z", "2026-08-08T22:10:00Z", role="exercise"),
    ]
    result = pr.fetch(_weekly_cfg(), reader=_weekly_reader(records),
                      trading_day_checker=lambda d: False, now=_now())
    cyc = _wcycles(result)["2026-08-08"]
    assert cyc.detail["execution_count"] == 2
    assert cyc.detail["attempts"] == 1
    assert cyc.detail["attempts_to_success"] == 1


# ------------------------------------------------------------ gate no-ops --


def test_a_gate_skip_execution_is_not_a_cadence_attempt():
    """The weekly trigger fires on more days than it runs; the run-day gate
    skip-succeeds in seconds. Counting those as cadence successes inflates the
    numerator with runs that did no work."""
    records = [_wexec("skip-1", "SUCCEEDED", "2026-08-08T09:00:00Z", "2026-08-08T09:00:02Z",
                      role="weekly")]
    result = pr.fetch(
        _weekly_cfg(noop_max_duration_seconds=30),
        reader=_weekly_reader(records), trading_day_checker=lambda d: False, now=_now(),
    )
    cyc = _wcycles(result)["2026-08-08"]
    assert cyc.state == pr.NEVER_FIRED
    assert cyc.detail["gate_noop_count"] == 1
    assert cyc.detail["attempts"] == 0
    assert _signal(result, "first-attempt-success-rate").detail["denominator"] == 0


def test_without_the_declared_noop_budget_every_execution_counts():
    records = [_wexec("skip-1", "SUCCEEDED", "2026-08-08T09:00:00Z", "2026-08-08T09:00:02Z",
                      role="weekly")]
    result = pr.fetch(_weekly_cfg(), reader=_weekly_reader(records),
                      trading_day_checker=lambda d: False, now=_now())
    assert _wcycles(result)["2026-08-08"].state == pr.SUCCEEDED


# ------------------------------------------------------------ stage depth --


STAGES = ("MorningEnrich", "ResearchPredictorParallel", "PredictorTraining", "Evaluator")


def test_stage_reached_renders_how_far_the_run_got():
    records = [
        _wexec("run-1", "FAILED", "2026-08-01T09:00:00Z", "2026-08-01T09:13:00Z", role="weekly",
               entered=["MorningEnrich"]),
        _wexec("run-2", "FAILED", "2026-08-08T09:00:00Z", "2026-08-08T12:34:00Z", role="weekly",
               entered=["MorningEnrich", "ResearchPredictorParallel", "PredictorTraining"]),
    ]
    result = pr.fetch(_weekly_cfg(stage_states=list(STAGES)), reader=_weekly_reader(records),
                      trading_day_checker=lambda d: False, now=_now())
    cycles = _wcycles(result)
    assert cycles["2026-08-01"].detail["stage_reached"] == "MorningEnrich"
    assert cycles["2026-08-01"].detail["stage_depth"] == 1
    # The same red, three stages further in — progress, not another red.
    assert cycles["2026-08-08"].detail["stage_reached"] == "PredictorTraining"
    assert cycles["2026-08-08"].detail["stage_depth"] == 3
    assert cycles["2026-08-08"].detail["stage_count"] == 4


def test_stage_fields_are_absent_when_no_stages_are_declared():
    records = [_wexec("run-1", "FAILED", "2026-08-08T09:00:00Z", "2026-08-08T09:13:00Z",
                      role="weekly", entered=["MorningEnrich"])]
    result = pr.fetch(_weekly_cfg(), reader=_weekly_reader(records),
                      trading_day_checker=lambda d: False, now=_now())
    assert "stage_reached" not in _wcycles(result)["2026-08-08"].detail


def test_history_attach_is_bounded_by_cycle_days_not_listed_executions(monkeypatch):
    """config-I7067: GetExecutionHistory is O(cycle days), not O(list_executions).

    Measured defect: 120 listed executions → 255s of history for a strip that
    renders depth for six. The attach subset must be the first cadence attempt
    per windowed cycle day.
    """
    calls: list[str] = []

    def fake_history_reader(_names):
        def attach(_region, records):
            for r in records:
                calls.append(r["name"])
                r.setdefault("entered_states", ["MorningEnrich"])
        return attach

    monkeypatch.setattr(pr, "history_reader_for", fake_history_reader)

    records = []
    for day in ("2026-07-25", "2026-08-01", "2026-08-08"):
        for i in range(10):
            records.append(_wexec(
                f"{day}-run-{i}", "FAILED",
                f"{day}T{9 + i:02d}:00:00Z", f"{day}T{9 + i:02d}:13:00Z",
                role="weekly",
            ))
    for i in range(10):
        records.append(_wexec(
            f"out-{i}", "FAILED",
            f"2026-07-18T{9 + i:02d}:00:00Z", f"2026-07-18T{9 + i:02d}:13:00Z",
            role="weekly",
        ))

    result = pr.fetch(
        _weekly_cfg(stage_states=list(STAGES)),
        reader=_weekly_reader(records),
        trading_day_checker=lambda d: False,
        now=_now(),
    )
    assert len(calls) <= 3
    assert set(calls) == {
        "2026-07-25-run-0", "2026-08-01-run-0", "2026-08-08-run-0",
    }
    assert _wcycles(result)["2026-08-08"].detail["stage_reached"] == "MorningEnrich"
    assert _wcycles(result)["2026-08-08"].detail["stage_depth"] == 1


# ---------------------------------------------------------------- cutovers --


def test_cutover_is_marked_on_the_cycle_it_lands_on():
    result = pr.fetch(
        _weekly_cfg(cutovers=[{"date": "2026-08-08", "label": "per-stage spot repoint",
                               "ref": "nousergon-data#1269"}]),
        reader=_weekly_reader([]), trading_day_checker=lambda d: False, now=_now(),
    )
    cycles = _wcycles(result)
    assert cycles["2026-08-08"].detail["cutover"]["label"] == "per-stage spot repoint"
    assert cycles["2026-08-08"].detail["cutover"]["ref"] == "nousergon-data#1269"
    assert "cutover" not in cycles["2026-08-01"].detail


# ---------------------------------------------------------- revisit trigger --


def test_rerun_revisit_trigger_fires_above_the_threshold():
    """sf-pipeline-policy.md §1: >1 operator rerun to reach a complete
    non-degraded terminal. A declared trigger with no detector fires only when
    somebody notices."""
    records = [
        _wexec("run-1", "FAILED", "2026-08-08T09:00:00Z", "2026-08-08T09:13:00Z", role="weekly"),
        _wexec("rr-1", "FAILED", "2026-08-08T10:00:00Z", "2026-08-08T10:20:00Z", role="watch-rerun"),
        _wexec("rr-2", "SUCCEEDED", "2026-08-08T11:00:00Z", "2026-08-08T14:00:00Z", role="watch-rerun"),
    ]
    result = pr.fetch(_weekly_cfg(), reader=_weekly_reader(records),
                      trading_day_checker=lambda d: False, now=_now())
    sig = _signal(result, "rerun-revisit-trigger")
    assert sig.state == "firing"
    assert sig.detail["breaches"][0]["date"] == "2026-08-08"
    assert sig.detail["fields"]["cycles_over_rerun_threshold"]["value"] == 1


def test_rerun_trigger_clear_on_a_first_attempt_success():
    records = [_wexec("run-1", "SUCCEEDED", "2026-08-08T09:00:00Z", "2026-08-08T12:00:00Z",
                      role="weekly")]
    result = pr.fetch(_weekly_cfg(), reader=_weekly_reader(records),
                      trading_day_checker=lambda d: False, now=_now())
    assert _signal(result, "rerun-revisit-trigger").state == "clear"


def test_a_cycle_that_never_succeeded_breaches_the_rerun_trigger():
    """Zero reruns that worked is not evidence the threshold was respected."""
    records = [_wexec("run-1", "FAILED", "2026-08-08T09:00:00Z", "2026-08-08T09:13:00Z",
                      role="weekly")]
    result = pr.fetch(_weekly_cfg(), reader=_weekly_reader(records),
                      trading_day_checker=lambda d: False, now=_now())
    assert _signal(result, "rerun-revisit-trigger").state == "firing"


# ------------------------------------------------- weekday path is unchanged --


def test_existing_weekday_config_is_unaffected_by_the_new_keys():
    records = [
        _exec("run-1", "SUCCEEDED", "2026-08-03T13:00:00Z", "2026-08-03T13:12:00Z", role="daily"),
    ]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=_now())
    cycles = _cycles_by_date(result)
    assert set(cycles) == {"2026-08-03", "2026-08-04", "2026-08-05", "2026-08-07", "2026-08-10"}
    assert cycles["2026-08-03"].state == pr.SUCCEEDED
    assert cycles["2026-08-03"].detail["attempts"] == 1
    assert cycles["2026-08-03"].detail["attempts_to_success"] == 1


# ===================================================================== #
# issue #8764 — scheduled-first-attempt-streak (alpha-engine-config#6967 #
# clause 1 as a number): walk most-recent-to-oldest, skip PENDING and    #
# HOLIDAY, count consecutive clean-first-attempt cycles.                #
# ===================================================================== #


def test_streak_through_a_pending_today_skips_it_without_breaking():
    """08-10 is today, still open (mid-day PT), zero executions — PENDING.
    08-03 is a real break; 08-04/05/07 are clean. PENDING must be skipped, not
    counted and not treated as a break."""
    mid_day = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)
    records = [
        _exec("run-1", "FAILED", "2026-08-03T13:00:00Z", "2026-08-03T13:05:00Z", role="daily"),
        _exec("run-2", "SUCCEEDED", "2026-08-04T13:00:00Z", "2026-08-04T13:12:00Z", role="daily"),
        _exec("run-3", "SUCCEEDED", "2026-08-05T13:00:00Z", "2026-08-05T13:12:00Z", role="daily"),
        _exec("run-4", "SUCCEEDED", "2026-08-07T13:00:00Z", "2026-08-07T13:12:00Z", role="daily"),
    ]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=mid_day)
    cycles = _cycles_by_date(result)
    assert cycles["2026-08-10"].state == pr.PENDING
    sig = _signal(result, "scheduled-first-attempt-streak", pipeline="preopen")
    assert sig.detail["fields"]["consecutive_cycles"]["value"] == 3
    assert sig.detail["last_break_date"] == "2026-08-03"
    assert sig.detail["censored"] is False
    assert sig.detail["window_cycles"] == 5


def test_rerun_recovered_day_breaks_the_streak():
    """A cycle a human rescued via `watch-rerun` is FAILED-recovered, not
    SUCCEEDED — 'a run that a human started is evidence about the human,' and
    the streak must end there even though the day eventually completed."""
    records = [
        _exec("run-1", "SUCCEEDED", "2026-08-03T13:00:00Z", "2026-08-03T13:12:00Z", role="daily"),
        _exec("run-2", "SUCCEEDED", "2026-08-04T13:00:00Z", "2026-08-04T13:12:00Z", role="daily"),
        _exec("run-3", "FAILED", "2026-08-05T13:00:00Z", "2026-08-05T13:05:00Z", role="daily"),
        _exec("run-4", "SUCCEEDED", "2026-08-05T14:00:00Z", "2026-08-05T14:12:00Z", role="watch-rerun"),
        _exec("run-5", "SUCCEEDED", "2026-08-07T13:00:00Z", "2026-08-07T13:12:00Z", role="daily"),
        _exec("run-6", "SUCCEEDED", "2026-08-10T13:00:00Z", "2026-08-10T13:12:00Z", role="daily"),
    ]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=_now())
    cycles = _cycles_by_date(result)
    assert cycles["2026-08-05"].state == pr.FAILED_RECOVERED
    sig = _signal(result, "scheduled-first-attempt-streak", pipeline="preopen")
    assert sig.detail["fields"]["consecutive_cycles"]["value"] == 2  # 08-10, 08-07 only
    assert sig.detail["last_break_date"] == "2026-08-05"
    assert sig.detail["censored"] is False
    assert sig.detail["fields"]["consecutive_cycles"]["value"] < sig.detail["window_cycles"]


def test_censored_streak_at_the_window_floor():
    """Every windowed cycle succeeds clean-first-attempt — the walk exhausts
    the window without finding a break. That is a lower bound, not a count:
    `censored: true`, `last_break_date: null`."""
    records = [
        _exec("run-1", "SUCCEEDED", "2026-08-03T13:00:00Z", "2026-08-03T13:12:00Z", role="daily"),
        _exec("run-2", "SUCCEEDED", "2026-08-04T13:00:00Z", "2026-08-04T13:12:00Z", role="daily"),
        _exec("run-3", "SUCCEEDED", "2026-08-05T13:00:00Z", "2026-08-05T13:12:00Z", role="daily"),
        _exec("run-4", "SUCCEEDED", "2026-08-07T13:00:00Z", "2026-08-07T13:12:00Z", role="daily"),
        _exec("run-5", "SUCCEEDED", "2026-08-10T13:00:00Z", "2026-08-10T13:12:00Z", role="daily"),
    ]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=_now())
    sig = _signal(result, "scheduled-first-attempt-streak", pipeline="preopen")
    assert sig.detail["fields"]["consecutive_cycles"]["value"] == 5
    assert sig.detail["window_cycles"] == 5
    assert sig.detail["censored"] is True
    assert sig.detail["last_break_date"] is None


def test_weekly_cadence_streak_weeks_math():
    """`cycles_per_week` is `len(cadence_weekdays)` for a `weekday` cadence — 1
    for a pipeline scheduled on a single Saturday — so `consecutive_weeks`
    equals `consecutive_cycles` here, not divided by the trading-day 5."""
    records = [
        _wexec("run-1", "FAILED", "2026-07-25T09:00:00Z", "2026-07-25T09:13:00Z", role="weekly"),
        _wexec("run-2", "SUCCEEDED", "2026-08-01T09:00:00Z", "2026-08-01T12:00:00Z", role="weekly"),
        _wexec("run-3", "SUCCEEDED", "2026-08-08T09:00:00Z", "2026-08-08T12:00:00Z", role="weekly"),
    ]
    result = pr.fetch(_weekly_cfg(), reader=_weekly_reader(records),
                      trading_day_checker=lambda d: False, now=_now())
    sig = _signal(result, "scheduled-first-attempt-streak")
    assert sig.detail["fields"]["consecutive_cycles"]["value"] == 2
    assert sig.detail["fields"]["consecutive_weeks"]["value"] == 2.0
    assert sig.detail["last_break_date"] == "2026-07-25"
    assert sig.detail["censored"] is False


def test_scheduled_first_attempt_streak_id_and_evidence():
    records = [
        _exec("run-1", "FAILED", "2026-08-10T13:00:00Z", "2026-08-10T13:05:00Z", role="daily"),
    ]
    result = pr.fetch(_cfg(), reader=_reader_for(records), trading_day_checker=_checker(), now=_now())
    sig = _signal(result, "scheduled-first-attempt-streak", pipeline="preopen")
    assert sig.id == "pipeline-reliability:preopen:scheduled-first-attempt-streak"
    assert sig.provenance.evidence == (
        "arn:aws:states:xx-test-1:000000000000:execution:fixture-preopen:run-1"
    )
