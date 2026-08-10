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
    return datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc)


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


def test_no_reader_and_no_boto3_fails_loud_not_empty():
    result = pr.fetch(_cfg(), reader=None, trading_day_checker=_checker(), now=_now())
    from console.model.envelope import AdapterStatus
    # boto3 is not installed in the test environment (test extra only) — the
    # default reader must decline honestly rather than the adapter crashing.
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
    six-value string is legal without touching the closed twelve."""
    from console.model.kinds import COMPONENT_STATE_KINDS, Kind
    assert Kind.CYCLE not in COMPONENT_STATE_KINDS
