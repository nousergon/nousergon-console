"""calendar-aware cadence translator (alpha-engine-config-I7050).

Covers the false-positive this module exists to prevent — a naive flat
`cadence_minutes` calling a Friday `eod_sf` artifact stale on Monday morning
— and the honesty rule that a cadence this module cannot translate is
excluded, never fabricated as fresh.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from console import calendar_cadence
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus
from console.model.kinds import Kind, State

# A Friday. 2026-08-14 is a Friday; 2026-08-15/16 are Sat/Sun; 2026-08-17 is
# the following Monday.
FRIDAY = datetime(2026, 8, 14, 16, 0, 0, tzinfo=timezone.utc)
MONDAY_MORNING = datetime(2026, 8, 17, 9, 0, 0, tzinfo=timezone.utc)
TUESDAY_MORNING = datetime(2026, 8, 18, 9, 0, 0, tzinfo=timezone.utc)


def _weekday_only_checker(date_str: str) -> bool:
    """A trading-day check with no holidays — weekends only. Enough to prove
    the calendar-awareness this module adds, independent of any real NYSE
    holiday table."""
    d = datetime.fromisoformat(date_str).date()
    return d.weekday() < 5


# ------------------------------------------------------------ continuous ---


def test_continuous_uses_the_declared_interval_plus_sla():
    result = calendar_cadence.effective_cadence_minutes(
        "continuous", interval_minutes=15, sla_minutes_after_cron=5,
    )
    assert result == 20.0


def test_continuous_with_no_interval_is_unauditable():
    assert calendar_cadence.effective_cadence_minutes("continuous") is None


# ---------------------------------------------------------- event_driven ---


def test_event_driven_never_yields_a_cadence():
    """`config/*_params`-class artifacts fire only on a real promotion event
    — modelling them as scheduled would be exactly the fabricated ceiling
    `principles.md` §2.7 forbids."""
    result = calendar_cadence.effective_cadence_minutes(
        "event_driven", now=MONDAY_MORNING, sla_minutes_after_cron=999999,
        interval_minutes=999999,
    )
    assert result is None


# -------------------------------------------------------------- unknown ----


def test_unknown_symbol_is_unauditable_not_guessed():
    assert calendar_cadence.effective_cadence_minutes("some_new_cadence", now=MONDAY_MORNING) is None


def test_no_cadence_at_all_is_unauditable():
    assert calendar_cadence.effective_cadence_minutes(None, now=MONDAY_MORNING) is None
    assert calendar_cadence.effective_cadence_minutes("", now=MONDAY_MORNING) is None


# ------------------------------------------------------------ saturday_sf --


def test_saturday_sf_spans_a_full_week_no_calendar_needed():
    """Tuesday, 4 days after last Saturday — well inside a weekly cadence,
    with no trading_day_checker supplied at all (Saturday isn't a trading
    day, so none should be required)."""
    tuesday = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)  # 2026-08-08 was Saturday
    minutes = calendar_cadence.effective_cadence_minutes("saturday_sf", now=tuesday)
    assert minutes is not None
    # last Saturday (08-08) 00:00 -> this Tuesday 12:00 = 3d12h = 5040 min,
    # plus the 1-day landing buffer.
    assert minutes == 5040.0 + 1440.0


# -------------------------------------------------- weekday_sf / eod_sf ----


def test_monday_morning_is_not_stale_for_a_friday_eod_sf_artifact():
    """The exact false-positive named in the issue: a naive flat 1-day
    cadence would call this stale (Fri 16:00 -> Mon 09:00 is 65 hours)."""
    cadence_minutes = calendar_cadence.effective_cadence_minutes(
        "eod_sf", now=MONDAY_MORNING, trading_day_checker=_weekday_only_checker,
    )
    assert cadence_minutes is not None

    idx = Index()
    idx.add_result(AdapterResult(
        name="artifact-registry", status=AdapterStatus.OK,
        # I7126: a source that declares its own poll cadence — §9.6 excludes a
        # row whose as-of source cannot say how often it looks.
        declared_cadence_seconds=60.0,
        entities=(Entity(
            kind=Kind.ARTIFACT, id="s3://bucket/eod-artifact.json", state="fresh",
            provenance=Provenance("object-store", as_of=FRIDAY.isoformat()),
            detail={"cadence_minutes": cadence_minutes},
        ),),
    ))
    result = idx.staleness_honesty(now=MONDAY_MORNING)
    assert result["of"] == 1
    assert result["count"] == 0, "a normal weekend gap must not read as a violation"


def test_a_genuine_multi_day_miss_is_still_caught():
    """A whole extra trading week missed — the calendar-awareness must not
    blind the check to a real gap."""
    stale_as_of = FRIDAY - timedelta(days=7)  # the Friday before last
    cadence_minutes = calendar_cadence.effective_cadence_minutes(
        "weekday_sf", now=MONDAY_MORNING, trading_day_checker=_weekday_only_checker,
    )
    idx = Index()
    idx.add_result(AdapterResult(
        name="artifact-registry", status=AdapterStatus.OK,
        # I7126: a source that declares its own poll cadence — §9.6 excludes a
        # row whose as-of source cannot say how often it looks.
        declared_cadence_seconds=60.0,
        entities=(Entity(
            kind=Kind.ARTIFACT, id="s3://bucket/missed-artifact.json", state="fresh",
            provenance=Provenance("object-store", as_of=stale_as_of.isoformat()),
            detail={"cadence_minutes": cadence_minutes},
        ),),
    ))
    result = idx.staleness_honesty(now=MONDAY_MORNING)
    assert result["of"] == 1
    assert result["count"] == 1, "a genuine week-plus-old artifact must still be caught"


def test_weekday_sf_with_no_trading_calendar_is_unauditable_not_assumed_fresh():
    result = calendar_cadence.effective_cadence_minutes(
        "eod_sf", now=MONDAY_MORNING, trading_day_checker=None,
    )
    # `default_trading_day_checker()` returns None unless `pandas-market-calendars`
    # is installed; this test asserts the refusal shape holds regardless of
    # whether that optional extra happens to be present in the test env.
    if calendar_cadence.default_trading_day_checker() is None:
        assert result is None


def test_tuesday_after_a_holiday_monday_still_resolves_via_the_checker():
    """A checker that also knows about a holiday (not just weekends) — proves
    the boundary search walks past however many non-trading days the injected
    calendar reports, not just a hardcoded weekend."""
    def checker(date_str: str) -> bool:
        d = datetime.fromisoformat(date_str).date()
        if d == datetime(2026, 8, 17).date():  # Monday holiday
            return False
        return d.weekday() < 5

    cadence_minutes = calendar_cadence.effective_cadence_minutes(
        "weekday_sf", now=TUESDAY_MORNING, trading_day_checker=checker,
    )
    assert cadence_minutes is not None
    idx = Index()
    idx.add_result(AdapterResult(
        name="artifact-registry", status=AdapterStatus.OK,
        # I7126: a source that declares its own poll cadence — §9.6 excludes a
        # row whose as-of source cannot say how often it looks.
        declared_cadence_seconds=60.0,
        entities=(Entity(
            kind=Kind.ARTIFACT, id="s3://bucket/x.json", state="fresh",
            provenance=Provenance("object-store", as_of=FRIDAY.isoformat()),
            detail={"cadence_minutes": cadence_minutes},
        ),),
    ))
    result = idx.staleness_honesty(now=TUESDAY_MORNING)
    assert result["count"] == 0


# ------------------------------------------------- resolve_key_template ----
# alpha-engine-config-I8770. `partition_lag_days` is the console-only escape
# hatch for a row whose declared `cadence` this module cannot translate into
# a calendar position — `continuous` is the live case, since an interval
# declares no calendar position by design (module docstring).


def test_continuous_templated_key_is_unresolved_with_no_lag_declared():
    """The residue this module exists to name honestly: `continuous` alone
    gives no calendar position, so the key stays unresolved."""
    key = calendar_cadence.resolve_key_template(
        "ops/scheduled_workflow_health/{date}.json",
        cadence="continuous", now=TUESDAY_MORNING,
    )
    assert key is None


def test_partition_lag_days_resolves_a_continuous_templated_key():
    """A genuine daily-all-calendar-days GHA cron declared `continuous`
    (no symbol in the fleet's closed cadence vocabulary covers it) resolves
    via its own declared lag rather than `cadence` at all."""
    key = calendar_cadence.resolve_key_template(
        "ops/scheduled_workflow_health/{date}.json",
        cadence="continuous", now=TUESDAY_MORNING, lag_days=1,
    )
    assert key == "ops/scheduled_workflow_health/2026-08-17.json"


def test_partition_lag_days_zero_is_todays_own_partition():
    key = calendar_cadence.resolve_key_template(
        "ops/x/{date}.json", cadence="continuous", now=TUESDAY_MORNING,
        lag_days=0,
    )
    assert key == "ops/x/2026-08-18.json"


def test_partition_lag_days_wins_over_cadence_and_resolver():
    """`lag_days` is the most specific override — checked before `cadence`/
    `resolver` are consulted at all, so it resolves even a symbol this module
    would otherwise call unauditable (`event_driven`)."""
    key = calendar_cadence.resolve_key_template(
        "ops/x/{date}.json", cadence="event_driven",
        resolver=calendar_cadence.RUN_DATE, now=TUESDAY_MORNING, lag_days=2,
    )
    assert key == "ops/x/2026-08-16.json"


def test_partition_lag_days_still_refuses_a_non_partition_placeholder():
    """The placeholder-shape guard runs first regardless of `lag_days` — a
    key templated on something other than a partition is never filled with a
    date, honest or not."""
    key = calendar_cadence.resolve_key_template(
        "models/{model_version}/weights.bin", cadence="continuous",
        now=TUESDAY_MORNING, lag_days=1,
    )
    assert key is None


# alpha-engine-config-I12857: a `*` glob segment names a family of keys, and
# HEADing the literal `*` is a guaranteed 404 that rendered as `absent`.
@pytest.mark.parametrize("template, cadence", [
    ("market_data/index_contributions/*/latest.json", "eod_sf"),
    ("market_data/index_contributions/*/{date}.json", "eod_sf"),
    ("predictor/diagnostics/oos_rows/*/latest.parquet", "saturday_sf"),
])
def test_a_glob_segment_is_never_resolved_to_a_key(template, cadence):
    assert calendar_cadence.resolve_key_template(
        template, cadence=cadence, now=TUESDAY_MORNING, lag_days=0,
    ) is None


def test_a_literal_key_without_a_glob_is_still_returned_unchanged():
    assert calendar_cadence.resolve_key_template(
        "ops/console/exceptions-latest.json", cadence="continuous", now=TUESDAY_MORNING,
    ) == "ops/console/exceptions-latest.json"


# ------------------------------------- continuous, session-bound producer ---
# alpha-engine-config-I12860: `trades/open_orders/latest.json` is written every
# 30 minutes inside `active_hours_utc: [14, 20]` on session days only. A flat
# `interval + sla` ceiling made it stale every night.

_OPEN_ORDERS = dict(interval_minutes=30, sla_minutes_after_cron=15,
                    run_calendar="market_hours", active_hours_utc=[14, 20])


def _ceiling(now, **row):
    return calendar_cadence.effective_cadence_minutes(
        "continuous", now=now, trading_day_checker=_weekday_only_checker, **row)


def test_market_hours_overnight_is_owed_the_time_since_the_window_closed():
    # Thursday 2026-08-13 window closed at 20:00Z; Friday 01:17Z is 317 min later.
    now = datetime(2026, 8, 14, 1, 17, tzinfo=timezone.utc)
    assert _ceiling(now, **_OPEN_ORDERS) == 317.0 + 45.0


def test_market_hours_mid_session_keeps_the_flat_ceiling():
    """A daemon that dies mid-session is still caught at interval + sla."""
    now = datetime(2026, 8, 14, 16, 0, tzinfo=timezone.utc)
    assert _ceiling(now, **_OPEN_ORDERS) == 45.0


def test_market_hours_leading_edge_anchors_on_the_previous_close():
    # 14:10Z Friday: less than interval + sla since the 14:00Z open, so the
    # floor still precedes the window. Thursday's 20:00Z close is 18h10m back.
    now = datetime(2026, 8, 14, 14, 10, tzinfo=timezone.utc)
    assert _ceiling(now, **_OPEN_ORDERS) == 18 * 60 + 10 + 45.0
    settled = datetime(2026, 8, 14, 14, 45, tzinfo=timezone.utc)
    assert _ceiling(settled, **_OPEN_ORDERS) == 45.0


def test_market_hours_weekend_reaches_back_to_fridays_close():
    # Monday 09:00Z: Friday 20:00Z close is 2 days 13 hours back.
    assert _ceiling(MONDAY_MORNING, **_OPEN_ORDERS) == (2 * 24 + 13) * 60 + 45.0


def test_active_hours_alone_implies_market_hours():
    now = datetime(2026, 8, 14, 1, 17, tzinfo=timezone.utc)
    row = dict(_OPEN_ORDERS)
    row.pop("run_calendar")
    assert _ceiling(now, **row) == 317.0 + 45.0


def test_trading_days_is_idle_only_on_non_session_days():
    row = dict(interval_minutes=60, sla_minutes_after_cron=0, run_calendar="trading_days")
    assert _ceiling(TUESDAY_MORNING, **row) == 60.0
    sunday = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    # Friday's whole-day window closed at Saturday 00:00Z, 36h before.
    assert _ceiling(sunday, **row) == 36 * 60 + 60.0


def test_all_days_and_undeclared_calendars_keep_the_flat_ceiling():
    now = datetime(2026, 8, 14, 1, 17, tzinfo=timezone.utc)
    assert _ceiling(now, interval_minutes=30, sla_minutes_after_cron=15,
                    run_calendar="all_days", active_hours_utc=[14, 20]) == 45.0
    assert _ceiling(now, interval_minutes=30, sla_minutes_after_cron=15) == 45.0


def test_a_malformed_window_or_no_calendar_never_loosens_the_ceiling():
    now = datetime(2026, 8, 14, 1, 17, tzinfo=timezone.utc)
    assert _ceiling(now, interval_minutes=30, sla_minutes_after_cron=15,
                    run_calendar="market_hours", active_hours_utc=[20, 14]) == 45.0
    assert calendar_cadence.effective_cadence_minutes(
        "continuous", now=now, trading_day_checker=lambda _d: False,
        **_OPEN_ORDERS) == 45.0


def test_a_daily_interval_is_left_to_the_flat_ceiling():
    now = datetime(2026, 8, 14, 1, 17, tzinfo=timezone.utc)
    assert _ceiling(now, interval_minutes=1440, sla_minutes_after_cron=0,
                    run_calendar="trading_days") == 1440.0


def test_apply_declared_cadence_reads_the_session_declaration():
    now = datetime(2026, 8, 14, 1, 17, tzinfo=timezone.utc)
    detail: dict = {}
    calendar_cadence.apply_declared_cadence(
        detail, {"cadence": "continuous", **_OPEN_ORDERS},
        now=now, trading_day_checker=_weekday_only_checker)
    assert detail["cadence_minutes"] == 317.0 + 45.0
