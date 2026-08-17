"""yaml-directory adapter tests — fixture registry, hermetic (no live source)."""
from __future__ import annotations

import os

from console.adapters import yaml_directory
from console.model.envelope import AdapterStatus
from console.model.kinds import Kind, State

EXAMPLE_REGISTRY = os.path.join(os.path.dirname(__file__), "..", "example", "registry.d")


def test_reads_one_component_per_file():
    result = yaml_directory.fetch({"path": EXAMPLE_REGISTRY, "id_field": "component_id"})
    assert result.status is AdapterStatus.OK
    ids = {e.id for e in result.entities}
    # comp-gamma (nousergon-console#56) demonstrates a `document-fields`
    # metrics binding — two deliberately split source documents combining
    # onto one component, alongside comp-alpha/comp-beta's plain rows.
    assert ids == {"comp-alpha", "comp-beta", "comp-gamma"}
    assert all(e.kind is Kind.COMPONENT for e in result.entities)


def test_facets_mapped_from_identity_fields():
    result = yaml_directory.fetch({"path": EXAMPLE_REGISTRY, "id_field": "component_id"})
    alpha = next(e for e in result.entities if e.id == "comp-alpha")
    assert alpha.facets.get("substrate") == "lambda"
    assert alpha.facets.get("repo") == "example-repo"


def test_missing_directory_is_failed_state_not_exception():
    result = yaml_directory.fetch({"path": "/no/such/dir", "id_field": "component_id"})
    assert result.status is AdapterStatus.FAILED
    assert "all" in result.unavailable


def test_declared_lifecycle_renders_the_declared_state(tmp_path):
    (tmp_path / "old.yaml").write_text(
        "component_id: comp-old\nlifecycle: retired\nowner: x\n"
    )
    result = yaml_directory.fetch({"path": str(tmp_path), "id_field": "component_id"})
    ent = result.entities[0]
    # §8.3: DISABLED/DEPRECATED/RETIRED are DECLARED in the registry and
    # nothing else may produce them. ABSENT is a different finding entirely —
    # the registry expects it and the substrate lacks it.
    assert ent.state is State.RETIRED


def test_registry_row_is_declared_not_measured(tmp_path):
    # A registry declaration has no telemetry: state is UNKNOWN, as_of is None
    # (declared absence, §2.3), never a guessed freshness.
    (tmp_path / "c.yaml").write_text("component_id: comp-x\nlifecycle: in-service\n")
    result = yaml_directory.fetch({"path": str(tmp_path), "id_field": "component_id"})
    ent = result.entities[0]
    # An in-service row with no observation is UNREPORTED — "registered, in
    # service, emitting nothing", which IS the transparency-gap count. It is
    # never UNKNOWN: §8.3 forbids that escape hatch by name.
    assert ent.state is State.UNREPORTED
    assert ent.provenance.as_of is None


# ------------------- symbolic (calendar-aware) cadence, I7050/I7060 ----------
#
# The registry directory accepts the SAME `cadence:` grammar `declared-registry`
# already accepts, through the same translator (`console/calendar_cadence.py`).
# Why a component row needs it at all: most of the fleet's silent Lambdas are
# TRADING-DAY pipeline stages. Declaring `cadence_minutes: 1440` on one makes
# every Saturday and Sunday read as MISSED, so before this the only available
# declarations were a false alarm and silence — and silence is exactly the
# UNREPORTED transparency gap the cadence exists to close.

from datetime import datetime, timedelta, timezone  # noqa: E402

from console.index.cadence_state import resolve_cadence_state  # noqa: E402
from console.model.entity import Entity, Provenance  # noqa: E402

#: A weekday-only trading calendar — injected, so the test never depends on
#: whether the optional `pandas-market-calendars` extra is installed.
def _weekday_checker(day: str) -> bool:
    from datetime import date
    return date.fromisoformat(day).weekday() < 5


_SUNDAY = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
_FRIDAY_RUN = datetime(2026, 8, 14, 21, 0, tzinfo=timezone.utc)


def _fetch_one(tmp_path, body: str, **kwargs):
    (tmp_path / "c.yaml").write_text(body)
    result = yaml_directory.fetch(
        {"path": str(tmp_path), "id_field": "component_id"}, **kwargs
    )
    return result.entities[0]


def test_symbolic_cadence_becomes_a_numeric_cadence_minutes(tmp_path):
    ent = _fetch_one(
        tmp_path,
        "component_id: comp-weekday\nlifecycle: in-service\n"
        "cadence: weekday_sf\nsla_minutes_after_cron: 30\n",
        now=_SUNDAY, trading_day_checker=_weekday_checker,
    )
    assert isinstance(ent.detail["cadence_minutes"], float)
    assert ent.detail["cadence_minutes"] > 0


def test_a_literal_cadence_minutes_is_never_overridden_by_a_symbol(tmp_path):
    """§5.1: a declaration wins its own field. Every row already carrying a
    literal count keeps it byte-for-byte, so this change moves no existing row."""
    ent = _fetch_one(
        tmp_path,
        "component_id: comp-both\nlifecycle: in-service\n"
        "cadence: weekday_sf\ncadence_minutes: 42\n",
        now=_SUNDAY, trading_day_checker=_weekday_checker,
    )
    assert ent.detail["cadence_minutes"] == 42


def test_event_driven_gets_no_cadence_minutes_at_all(tmp_path):
    """A component fired only by an event has no schedule to miss. It stays
    excluded from the audited population rather than being handed a
    manufactured one (`principles.md` §2.7)."""
    ent = _fetch_one(
        tmp_path,
        "component_id: comp-event\nlifecycle: in-service\ncadence: event_driven\n",
        now=_SUNDAY, trading_day_checker=_weekday_checker,
    )
    assert "cadence_minutes" not in ent.detail


def test_continuous_needs_its_interval_minutes(tmp_path):
    """`continuous` with no declared interval is unauditable, not hourly."""
    without = _fetch_one(
        tmp_path, "component_id: comp-c\nlifecycle: in-service\ncadence: continuous\n",
    )
    assert "cadence_minutes" not in without.detail
    with_interval = _fetch_one(
        tmp_path,
        "component_id: comp-c\nlifecycle: in-service\n"
        "cadence: continuous\ninterval_minutes: 15\n",
    )
    assert with_interval.detail["cadence_minutes"] == 15


def test_an_unknown_symbol_is_unauditable_not_guessed(tmp_path):
    ent = _fetch_one(
        tmp_path,
        "component_id: comp-x\nlifecycle: in-service\ncadence: every_third_tuesday\n",
        now=_SUNDAY, trading_day_checker=_weekday_checker,
    )
    assert "cadence_minutes" not in ent.detail


def _silent_since(ent: Entity, last_run: datetime) -> Entity:
    """The merged shape `cloudwatch_metrics` + this adapter produce together:
    a declared cadence beside an observed silence (`index/cadence_state.py`)."""
    detail = dict(ent.detail)
    detail.update({"invocations": 0.0, "window_minutes": 1440,
                   "last_invocation": last_run.isoformat()})
    return Entity(kind=ent.kind, id=ent.id, state=ent.state,
                  provenance=Provenance(source="merged", as_of=None), detail=detail)


def test_a_weekday_component_silent_over_the_weekend_is_not_MISSED(tmp_path):
    """The acceptance case. Friday's run, read on Sunday: the trading calendar
    says no fire was expected in between, so this is HEALTHY."""
    ent = _fetch_one(
        tmp_path,
        "component_id: comp-weekday\nlifecycle: in-service\ncadence: weekday_sf\n",
        now=_SUNDAY, trading_day_checker=_weekday_checker,
    )
    placed = resolve_cadence_state(_silent_since(ent, _FRIDAY_RUN), now=_SUNDAY)
    assert placed.state is State.HEALTHY


def test_the_same_silence_under_a_flat_daily_cadence_would_false_flag(tmp_path):
    """The defect being removed, measured rather than asserted: the only
    declaration available before this change turns an ordinary weekend into a
    MISSED page. Both rows, one clock, opposite verdicts."""
    flat = _fetch_one(
        tmp_path,
        "component_id: comp-weekday\nlifecycle: in-service\n"
        "cadence_minutes: 1440\ncadence_source: the pipeline's weekday cron\n",
    )
    placed = resolve_cadence_state(_silent_since(flat, _FRIDAY_RUN), now=_SUNDAY)
    assert placed.state is State.MISSED


def test_a_genuine_weekday_miss_is_still_caught(tmp_path):
    """Widening for the calendar must not blind the check: a weekday component
    last seen two full trading weeks ago is still MISSED."""
    ent = _fetch_one(
        tmp_path,
        "component_id: comp-weekday\nlifecycle: in-service\ncadence: weekday_sf\n",
        now=_SUNDAY, trading_day_checker=_weekday_checker,
    )
    placed = resolve_cadence_state(
        _silent_since(ent, _SUNDAY - timedelta(days=14)), now=_SUNDAY
    )
    assert placed.state is State.MISSED
