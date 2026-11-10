"""Hermetic coverage of the optional trading-calendar default and its cache."""
from __future__ import annotations

import sys
from types import ModuleType

import pytest

from console.trading_calendar import default_trading_day_checker


def test_missing_calendar_library_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", None)

    assert default_trading_day_checker() is None


def test_calendar_checker_caches_dates_per_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    calendar_calls: list[str] = []
    valid_days_calls: list[tuple[str, str]] = []
    trading_day = "2026-08-14"
    closed_day = "2026-08-15"

    class FakeCalendar:
        def valid_days(self, *, start_date: str, end_date: str) -> list[str]:
            valid_days_calls.append((start_date, end_date))
            return [start_date] if start_date == trading_day else []

    def get_calendar(name: str) -> FakeCalendar:
        calendar_calls.append(name)
        return FakeCalendar()

    module = ModuleType("pandas_market_calendars")
    setattr(module, "get_calendar", get_calendar)
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", module)

    checker = default_trading_day_checker()
    assert checker is not None
    assert calendar_calls == ["XNYS"]
    assert checker(trading_day) is True
    assert checker(trading_day) is True
    assert checker(closed_day) is False
    assert checker(closed_day) is False
    assert valid_days_calls == [(trading_day, trading_day), (closed_day, closed_day)]

    second_checker = default_trading_day_checker()
    assert second_checker is not None
    assert calendar_calls == ["XNYS", "XNYS"]
    assert second_checker(trading_day) is True
    assert second_checker(closed_day) is False
    assert valid_days_calls == [
        (trading_day, trading_day),
        (closed_day, closed_day),
        (trading_day, trading_day),
        (closed_day, closed_day),
    ]
