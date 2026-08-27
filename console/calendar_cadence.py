"""Calendar-aware cadence translator — symbolic recurring cadences
(`console-policy.md` §5.2, alpha-engine-config-I7050).

`console/index/numbers.py::staleness_honesty()` audits any entity carrying a
numeric `detail["cadence_minutes"]` against its `provenance.as_of`. That
mechanism is correct and unit-tested, but it is only reachable by an entity
that already HAS a flat minute count. Several real registries — the fleet's
own `ARTIFACT_REGISTRY.yaml` among them — declare a CALENDAR-AWARE cadence
instead: a symbol naming *when* a thing is expected to refresh, not how many
literal minutes apart. Flattening `weekday_sf`/`eod_sf` to "1 day" makes every
Monday morning read as stale relative to Friday's last refresh — a false
alarm, and the opposite failure from the one honesty checks exist to prevent
(`principles.md` §2.7: an absence must render as itself, never manufactured).

Five symbols — generic across any deployment that declares a weekly /
trading-day-recurring / continuously-refreshed / event-only cadence (they
happen to match the fleet's own Step-Functions naming, the same way
`console/adapters/object_store.py`'s own `cadence: "1h"/"30m"` tokens are a
generic format that happens to suit this fleet):

- ``continuous``   — refreshes on a fixed literal interval
  (``interval_minutes``, required). No calendar awareness needed; this is
  what `object_store`'s own ``cadence: "Nm"`` config already expresses, so
  this symbol is a thin pass-through.
- ``saturday_sf``  — refreshes once a WEEK, on Saturday. Calendar-day
  anchored, not trading-day anchored — Saturday is never a trading day, so
  the NYSE calendar has nothing to say about it.
- ``weekday_sf`` / ``eod_sf`` — refreshes once per TRADING day. Needs a
  trading calendar to tell a genuine multi-day miss apart from an ordinary
  weekend or holiday gap.
- ``event_driven`` — no schedule AT ALL, by declaration
  (`ARTIFACT_REGISTRY.yaml` header; the fleet's four ``config/*_params``
  artifacts are the canonical case — fired only by an actual
  promotion/demotion event, never on a timer). Deliberately returns ``None``
  — never rendered as a manufactured cadence, and therefore excluded from
  `staleness_honesty`'s denominator exactly like any other unauditable row
  (§5.3). Their liveness anchor is a different artifact
  (``config_apply_audit``), not a cadence on themselves.

**Design: the returned value already bakes `now` in.** Rather than
re-deriving a trading-day-adjusted AGE inside `staleness_honesty()` itself
(which is generic, source-agnostic, and already unit-tested against a flat
`cadence_minutes` — 8/8 passing before this module existed), this computes,
at adapter-fetch time, an EFFECTIVE minute ceiling that already accounts for
the calendar gap between `now` and the most recent moment a refresh was
expected. `staleness_honesty` then performs its ordinary flat comparison
against that ceiling: no change to the tested core, and no second age
computation that could quietly disagree with the first.

**Never fabricates.** An unknown symbol, a `continuous` cadence with no
declared `interval_minutes`, or a trading-day symbol with no reachable
trading calendar all return ``None`` — "unauditable", never "assume fresh".
"""
from __future__ import annotations

import re as _re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from .trading_calendar import TradingDayChecker, default_trading_day_checker

SATURDAY_SF = "saturday_sf"
WEEKDAY_SF = "weekday_sf"
EOD_SF = "eod_sf"
CONTINUOUS = "continuous"
EVENT_DRIVEN = "event_driven"

#: Symbols this module knows how to translate. Anything else is honestly
#: "unknown cadence" — never guessed, never fabricated (§5.3/§2.7).
KNOWN_SYMBOLS = frozenset({SATURDAY_SF, WEEKDAY_SF, EOD_SF, CONTINUOUS, EVENT_DRIVEN})

#: One nominal trading day of slack folded into the trading-day-anchored
#: symbols — the time it takes an artifact to actually land WITHIN its
#: expected day. `staleness_honesty`'s own `staleness_factor` multiplies on
#: top of this at audit time; this is the baseline, not the grace.
_ONE_DAY_MINUTES = 1440.0

#: Saturday = weekday() 5 (Monday is 0).
_SATURDAY = 5

#: How far back a boundary search may look before giving up and declaring
#: unauditable rather than spinning — no real trading calendar goes this long
#: with zero sessions, and no real weekly cadence needs more than a week to
#: find its own Saturday.
_TRADING_DAY_LOOKBACK_LIMIT = 30
_WEEKDAY_LOOKBACK_LIMIT = 8


def effective_cadence_minutes(
    cadence: str | None,
    *,
    now: datetime | None = None,
    trading_day_checker: TradingDayChecker | None = None,
    sla_minutes_after_cron: float | None = None,
    interval_minutes: float | None = None,
) -> float | None:
    """Translate a symbolic cadence into a minute ceiling valid AS OF `now`.

    Returns ``None`` when the cadence cannot be honestly translated —
    unknown symbol, `event_driven` (no schedule by declaration), `continuous`
    with no declared `interval_minutes`, or a trading-day symbol with no
    reachable trading calendar. `None` means "excluded from the audited
    population" (§5.3), never "assume fresh".
    """
    if not cadence:
        return None
    symbol = str(cadence).strip().lower()
    if symbol not in KNOWN_SYMBOLS or symbol == EVENT_DRIVEN:
        return None

    grace = float(sla_minutes_after_cron or 0)

    if symbol == CONTINUOUS:
        if not interval_minutes:
            return None
        return float(interval_minutes) + grace

    now = now or datetime.now(timezone.utc)

    if symbol == SATURDAY_SF:
        boundary = _most_recent_weekday_before(now.date(), _SATURDAY)
        if boundary is None:
            return None
        return _minutes_since(boundary, now) + _ONE_DAY_MINUTES + grace

    # weekday_sf / eod_sf — once per TRADING day.
    checker = trading_day_checker or default_trading_day_checker()
    if checker is None:
        # Declared unable rather than assuming every day is a trading day —
        # the latter would make a real Monday miss indistinguishable from an
        # ordinary weekend (module docstring; mirrors
        # `pipeline_reliability._default_trading_day_checker`'s own refusal).
        return None
    boundary = _most_recent_trading_day_before(now.date(), checker)
    if boundary is None:
        return None
    return _minutes_since(boundary, now) + _ONE_DAY_MINUTES + grace


def _minutes_since(boundary: date, now: datetime) -> float:
    boundary_dt = datetime(boundary.year, boundary.month, boundary.day, tzinfo=timezone.utc)
    return max(0.0, (now - boundary_dt).total_seconds() / 60.0)


def _most_recent_weekday_before(today: date, weekday: int) -> date | None:
    """The most recent calendar date < `today` whose `date.weekday() == weekday`."""
    cursor = today - timedelta(days=1)
    for _ in range(_WEEKDAY_LOOKBACK_LIMIT):
        if cursor.weekday() == weekday:
            return cursor
        cursor -= timedelta(days=1)
    return None  # pragma: no cover - unreachable, weekday cycles every 7 days


def _most_recent_trading_day_before(today: date, checker: TradingDayChecker) -> date | None:
    """The most recent trading date < `today`, per `checker`.

    Bounded lookback so a checker that never returns True fails loud
    (`None`) rather than spinning.
    """
    cursor = today - timedelta(days=1)
    for _ in range(_TRADING_DAY_LOOKBACK_LIMIT):
        if checker(cursor.isoformat()):
            return cursor
        cursor -= timedelta(days=1)
    return None


def apply_declared_cadence(
    detail: dict[str, Any],
    entry: Mapping[str, Any],
    *,
    now: datetime | None = None,
    trading_day_checker: TradingDayChecker | None = None,
) -> None:
    """Symbolic `cadence` -> the numeric `detail["cadence_minutes"]` the index
    audits (alpha-engine-config-I7050, I7060).

    ONE grammar, TWO callers — the `records_shape.py` precedent (#100). Both
    registry adapters read the identical declaration shape:

    - `adapters/declared_registry.py` — one YAML document, many entries of one
      kind (the fleet's `ARTIFACT_REGISTRY.yaml`).
    - `adapters/yaml_directory.py` — a directory of one file per Component
      (the fleet's `governance/observability.d/`).

    Forking it — leaving the directory adapter numeric-only because "a
    component row can just state its minutes" — is what made a weekday Lambda
    undeclarable: a flat `1440` on a `weekday_sf` stage false-flags `MISSED`
    every Saturday and Sunday, so the only honest declaration available was
    none at all, and the row stayed in the transparency gap
    (`observability-policy.md` §8.3).

    A row that already declares a plain numeric `cadence_minutes` keeps it
    VERBATIM (§5.1's declaration-wins-its-own-field rule) — this only fills
    the gap for a row whose cadence is a calendar symbol, never overriding an
    explicit value. When the symbol cannot be honestly translated
    (`event_driven`, an unknown symbol, a trading-day symbol with no reachable
    calendar) nothing is added: the row stays excluded from the audited
    population (§5.3), never faked into looking fresh.
    """
    if detail.get("cadence_minutes"):
        return
    cadence = entry.get("cadence")
    if not cadence:
        return
    minutes = effective_cadence_minutes(
        cadence,
        now=now,
        trading_day_checker=trading_day_checker,
        sla_minutes_after_cron=entry.get("sla_minutes_after_cron"),
        interval_minutes=entry.get("interval_minutes"),
    )
    if minutes is not None:
        detail["cadence_minutes"] = minutes


# ------------------------------------------------- last expected partition --
#
# alpha-engine-config-I8765. A declared key template like
# `signals/{trading_day}/signals.json` names a FAMILY of objects, one per
# partition. To observe it — to HEAD one key and get a real fresh/stale/absent
# answer rather than defaulting the whole row to a finding — the console must
# resolve the placeholder to the LAST PARTITION THE DECLARATION EXPECTS.
#
# That is two steps, and keeping them separate is what makes the rule
# auditable: (1) when did this cadence last fire, (2) which partition does a
# run of that cadence write. The second is NOT derivable from the first, and
# assuming it is manufactures absences at scale. Measured against the live
# bucket 2026-08-27: every `saturday_sf` artifact is keyed by the FRIDAY, not
# by the Saturday its pipeline runs on — `market_data/weekly/2026-08-22/` does
# not exist at all — while two rows of the same cadence are keyed by the run
# date itself. So the mapping is declared per key, from a closed vocabulary,
# and never inferred from the cadence alone.

#: The partition a run writes, relative to the run itself. Closed, add-by-PR.
RUN_DATE = "run-date"
LAST_TRADING_DAY_BEFORE_RUN = "last-trading-day-before-run"
PARTITION_RESOLVERS = frozenset({RUN_DATE, LAST_TRADING_DAY_BEFORE_RUN})

#: Placeholders a partition date may fill. A template carrying any OTHER
#: placeholder is left unresolved (`None`) rather than filled with a date that
#: is not what the name says — an artifact keyed by ticker or model version is
#: not a partition, and a wrong key HEADs to a false `absent`.
PARTITION_PLACEHOLDERS = frozenset({"date", "trading_day", "partition"})

_PLACEHOLDER = _re.compile(r"\{(\w+)\}")

#: ISO-8601 is the fleet's own partition spelling and the only one this
#: resolves today; a deployment whose partitions are `YYYYMMDD` declares
#: `date_format` rather than getting a second resolver.
DEFAULT_DATE_FORMAT = "%Y-%m-%d"


def last_expected_run_date(
    cadence: str | None,
    *,
    now: datetime | None = None,
    trading_day_checker: TradingDayChecker | None = None,
) -> date | None:
    """The most recent date this cadence was expected to FIRE, before `now`.

    `None` — never a guess — for `continuous` (an interval, not a calendar
    position: today's partition may legitimately not exist yet and yesterday's
    is legitimately old, so neither is "the last expected one"), for
    `event_driven` (no schedule by declaration), for an unknown symbol, and for
    a trading-day symbol with no reachable calendar.
    """
    if not cadence:
        return None
    symbol = str(cadence).strip().lower()
    now = now or datetime.now(timezone.utc)
    if symbol == SATURDAY_SF:
        return _most_recent_weekday_before(now.date(), _SATURDAY)
    if symbol in (WEEKDAY_SF, EOD_SF):
        checker = trading_day_checker or default_trading_day_checker()
        if checker is None:
            return None
        return _most_recent_trading_day_before(now.date(), checker)
    return None


def resolve_partition_date(
    cadence: str | None,
    resolver: str,
    *,
    now: datetime | None = None,
    trading_day_checker: TradingDayChecker | None = None,
) -> date | None:
    """The partition date the last expected run of `cadence` wrote."""
    run_date = last_expected_run_date(
        cadence, now=now, trading_day_checker=trading_day_checker)
    if run_date is None:
        return None
    if resolver == RUN_DATE:
        return run_date
    if resolver == LAST_TRADING_DAY_BEFORE_RUN:
        checker = trading_day_checker or default_trading_day_checker()
        if checker is None:
            return None
        return _most_recent_trading_day_before(run_date, checker)
    return None  # an unknown resolver is unauditable, never a default


def resolve_key_template(
    template: str,
    *,
    cadence: str | None,
    resolver: str = RUN_DATE,
    now: datetime | None = None,
    trading_day_checker: TradingDayChecker | None = None,
    date_format: str = DEFAULT_DATE_FORMAT,
) -> str | None:
    """`signals/{trading_day}/signals.json` -> `signals/2026-08-21/signals.json`.

    Returns the template unchanged when it carries no placeholder, and `None`
    when it carries one that cannot be honestly filled — an unresolvable
    cadence, an unknown resolver, or a placeholder that does not name a
    partition. `None` means "do not look", which leaves the row declared and
    unobserved: a visible coverage gap, and strictly better than HEADing a key
    the fleet never writes and rendering the 404 as a finding.
    """
    names = set(_PLACEHOLDER.findall(template))
    if not names:
        return template
    if names - PARTITION_PLACEHOLDERS:
        return None
    partition = resolve_partition_date(
        cadence, resolver, now=now, trading_day_checker=trading_day_checker)
    if partition is None:
        return None
    stamp = partition.strftime(date_format)
    return _PLACEHOLDER.sub(stamp, template)
