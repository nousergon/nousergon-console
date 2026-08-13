"""calendar-aware cadence translator (alpha-engine-config-I7050).

Covers the false-positive this module exists to prevent — a naive flat
`cadence_minutes` calling a Friday `eod_sf` artifact stale on Monday morning
— and the honesty rule that a cadence this module cannot translate is
excluded, never fabricated as fresh.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
