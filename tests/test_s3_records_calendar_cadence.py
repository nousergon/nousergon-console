"""s3-records takes a calendar cadence (alpha-engine-config-I13052).

A weekly source's deadline is a calendar instant, not "N days after the last
object". These pin the three things that make it one: a declared
`deadline: {weekday, hour_utc}` beside `saturday_sf` flips a row exactly at
that instant and at no other, a weekday symbol is not stale over a weekend,
and a malformed declaration fails the source instead of being guessed.
Synthetic names only (this repo is public).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from console import calendar_cadence
from console.adapters import s3_records
from console.model.envelope import AdapterStatus

BUCKET = "fixture-bucket"
KEY = "report/{day}/weekly.json"
# 2026-10-31 is a Saturday; the arc finished at 12:01Z.
WEEK_1 = ("2026-10-30", "2026-10-31T12:01:00+00:00")
WEEK_2 = ("2026-11-06", "2026-11-07T12:01:00+00:00")


def _cfg(**extra):
    return {
        "bucket": BUCKET, "prefix": "report/", "kind": "cycle",
        "key_pattern": r"^report/(?P<day>\d{4}-\d{2}-\d{2})/weekly\.json$",
        "id_template": "week:{day}", "as_of_field": "finished",
        "cadence": "saturday_sf", **extra,
    }


def _fetch(weeks, now, **extra):
    objects = {KEY.format(day=d): {"finished": f} for d, f in weeks}
    return s3_records.fetch(
        _cfg(**extra),
        lister=lambda b, p: [(k, v["finished"]) for k, v in objects.items()],
        reader=lambda b, k: objects[k],
        now=now,
    )


def _state(result, day):
    return {e.id: e.state for e in result.entities}[f"week:{day}"]


DEADLINE = {"deadline": {"weekday": "sunday", "hour_utc": 12}}


@pytest.mark.parametrize("now,expected", [
    # Saturday before the next arc: last week's object is still this week's.
    (datetime(2026, 11, 7, 6, 0, tzinfo=timezone.utc), "fresh"),
    # Sunday morning, no new object yet: the deadline has not passed.
    (datetime(2026, 11, 8, 11, 59, tzinfo=timezone.utc), "fresh"),
    # Exactly at and after Sunday 12:00Z with no new object: stale.
    (datetime(2026, 11, 8, 12, 0, 1, tzinfo=timezone.utc), "stale"),
    (datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc), "stale"),
])
def test_a_missing_week_goes_stale_exactly_at_the_declared_deadline(now, expected):
    result = _fetch([WEEK_1], now, **DEADLINE)
    assert result.status is AdapterStatus.OK
    assert _state(result, "2026-10-30") == expected


def test_the_deadline_does_not_move_with_the_arcs_finish_time():
    # A late arc (Saturday 23:00Z) is not given until the following Monday,
    # which is what the flat `7d` x 8/7 approximation did.
    late = ("2026-10-30", "2026-10-31T23:00:00+00:00")
    now = datetime(2026, 11, 8, 13, 0, tzinfo=timezone.utc)
    assert _state(_fetch([late], now, **DEADLINE), "2026-10-30") == "stale"
    flat = _fetch([late], now, cadence="7d", staleness_factor=1.142857)
    assert _state(flat, "2026-10-30") == "fresh"


def test_the_week_that_arrived_is_fresh_after_the_deadline():
    now = datetime(2026, 11, 8, 13, 0, tzinfo=timezone.utc)
    result = _fetch([WEEK_1, WEEK_2], now, **DEADLINE)
    assert _state(result, "2026-11-06") == "fresh"
    assert _state(result, "2026-10-30") == "stale"


def test_an_arc_finishing_after_its_deadline_is_stale_until_it_lands():
    now = datetime(2026, 11, 8, 12, 30, tzinfo=timezone.utc)
    assert _state(_fetch([WEEK_1], now, **DEADLINE), "2026-10-30") == "stale"
    landed = ("2026-11-06", "2026-11-08T12:20:00+00:00")
    assert _state(_fetch([WEEK_1, landed], now, **DEADLINE), "2026-11-06") == "fresh"


def test_the_source_declares_the_weekly_period_not_the_moving_ceiling():
    for now in (datetime(2026, 11, 7, 6, tzinfo=timezone.utc),
                datetime(2026, 11, 9, 6, tzinfo=timezone.utc)):
        assert _fetch([WEEK_1], now, **DEADLINE).declared_cadence_seconds == 7 * 86400


def test_saturday_sf_without_a_deadline_flips_at_sunday_midnight():
    assert _state(_fetch([WEEK_1], datetime(2026, 11, 7, 23, 59, tzinfo=timezone.utc)),
                  "2026-10-30") == "fresh"
    assert _state(_fetch([WEEK_1], datetime(2026, 11, 8, 0, 1, tzinfo=timezone.utc)),
                  "2026-10-30") == "stale"


def test_a_weekday_symbol_is_not_stale_over_the_weekend():
    friday = [("2026-10-30", "2026-10-30T21:00:00+00:00")]
    sessions = lambda d: datetime.fromisoformat(d).weekday() < 5  # noqa: E731
    result = s3_records.fetch(
        _cfg(cadence="weekday_sf", staleness_factor=1.0),
        lister=lambda b, p: [(KEY.format(day=friday[0][0]), friday[0][1])],
        reader=lambda b, k: {"finished": friday[0][1]},
        now=datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc),
        trading_day_checker=sessions,
    )
    assert _state(result, "2026-10-30") == "fresh"
    assert result.declared_cadence_seconds == 86400


def test_event_driven_is_unauditable_never_fresh():
    result = _fetch([WEEK_1], datetime(2026, 11, 8, 13, tzinfo=timezone.utc),
                    cadence="event_driven")
    assert _state(result, "2026-10-30") == "no-cadence-declared"
    assert result.declared_cadence_seconds is None


@pytest.mark.parametrize("extra,bad", [
    ({"deadline": {"weekday": "sundy", "hour_utc": 12}}, "deadline"),
    ({"deadline": {"weekday": "sunday", "hour_utc": 24}}, "deadline"),
    ({"deadline": {"weekday": "sunday", "hour": 12}}, "deadline"),
    ({"deadline": {"weekday": "sunday"}, "cadence": "weekday_sf"}, "deadline"),
    ({"deadline": {"weekday": "sunday"}, "cadence": "7d"}, "deadline"),
    ({**DEADLINE, "staleness_factor": 1.5}, "staleness_factor"),
])
def test_a_malformed_deadline_fails_the_source(extra, bad):
    result = _fetch([WEEK_1], datetime(2026, 11, 8, 13, tzinfo=timezone.utc), **extra)
    assert result.status is AdapterStatus.FAILED
    assert bad in result.unavailable


def test_the_ceiling_only_flips_at_the_deadline_instant():
    # Over a fortnight at 10-minute steps, `as_of >= previous deadline` is the
    # whole verdict: compare the ceiling against that statement directly.
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    as_of = datetime(2026, 11, 7, 12, 1, tzinfo=timezone.utc)
    for step in range(16 * 144):
        now = start + timedelta(minutes=10 * step)
        if now < as_of:
            continue
        ceiling = calendar_cadence.weekly_deadline_ceiling_minutes(6, 12, now=now)
        fresh = (now - as_of).total_seconds() / 60.0 <= ceiling
        assert fresh == (now < datetime(2026, 11, 15, 12, tzinfo=timezone.utc)), now


def test_parse_weekly_deadline_accepts_names_and_numbers():
    assert calendar_cadence.parse_weekly_deadline({"weekday": "Sunday", "hour_utc": 12}) == (6, 12)
    assert calendar_cadence.parse_weekly_deadline({"weekday": 0}) == (0, 0)
    assert calendar_cadence.parse_weekly_deadline({"weekday": True}) is None
    assert calendar_cadence.parse_weekly_deadline("sunday") is None
