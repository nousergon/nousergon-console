"""Shared trading-day check — injectable, defaulting to `pandas-market-calendars`
when the optional `calendar` extra is installed (`console-policy.md` §2.3).

Lifted out of `console/adapters/pipeline_reliability.py` on its second adopter
(`console/calendar_cadence.py`, alpha-engine-config-I7050) per
`shared-code-policy.md`'s second-adoption trigger: one trading-day check,
reused, rather than two copies that drift.

Deliberately NOT `krepis.trading_calendar` / `nousergon_lib`: this repo is a
standalone public tool with zero fleet-specific dependencies (README "generic
over sources"; the `aws` extra is the only precedent for an optional
dependency, and it names no vendor's business logic). A private deployment
that wants the fleet's own calendar injects its own `trading_day_checker`
callable instead of relying on this default.
"""
from __future__ import annotations

from typing import Callable

#: date string (YYYY-MM-DD) -> is this a trading day. Injectable so tests run
#: hermetically with no live calendar library (groom-sweep §8.1).
TradingDayChecker = Callable[[str], bool]


def default_trading_day_checker() -> TradingDayChecker | None:
    """`pandas-market-calendars`-backed NYSE trading-day check, when the
    optional `calendar` extra is installed. `None` when it is not — callers
    must declare themselves unable rather than silently assuming every day
    is a trading day (the same refusal `pipeline_reliability` already makes).
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
