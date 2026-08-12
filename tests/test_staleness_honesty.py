"""§9.6 staleness honesty (console-policy.md §5.2, §9.6). Target 0.

Independently RE-DERIVES staleness from each entity's own declared cadence
and as-of, rather than trusting the state an adapter already assigned — an
honesty check that only trusted the thing it is checking would never catch
the bug it exists for: a row that is stale by its own declared cadence but
whose rendered value gives no sign of it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from console.index.graph import Index
from console.index.numbers import staleness_honesty
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus
from console.model.kinds import Kind, State

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


def _index_with(entity: Entity) -> Index:
    idx = Index()
    idx.add_result(AdapterResult(name="fixture", status=AdapterStatus.OK,
                                 entities=(entity,)))
    return idx


def test_a_row_silently_stale_is_a_violation():
    """5 minutes cadence, last reported an hour ago, but still rendering
    HEALTHY — the state gives no sign of the age at all."""
    stale_but_silent = Entity(
        kind=Kind.COMPONENT, id="comp-x", state=State.HEALTHY,
        provenance=Provenance("checks", as_of=(NOW - timedelta(hours=1)).isoformat()),
        detail={"cadence_minutes": 5},
    )
    result = staleness_honesty(_index_with(stale_but_silent), now=NOW)
    assert result["count"] == 1
    assert "comp-x" in result["violations"]


def test_a_row_disclosing_its_own_staleness_is_not_a_violation():
    disclosed = Entity(
        kind=Kind.COMPONENT, id="comp-y", state=State.MISSED,
        provenance=Provenance("checks", as_of=(NOW - timedelta(hours=1)).isoformat()),
        detail={"cadence_minutes": 5},
    )
    result = staleness_honesty(_index_with(disclosed), now=NOW)
    assert result["count"] == 0
    assert result["of"] == 1  # it WAS checked; it just passed


def test_a_fresh_row_is_not_a_violation():
    fresh = Entity(
        kind=Kind.COMPONENT, id="comp-z", state=State.HEALTHY,
        provenance=Provenance("checks", as_of=(NOW - timedelta(minutes=1)).isoformat()),
        detail={"cadence_minutes": 5},
    )
    result = staleness_honesty(_index_with(fresh), now=NOW)
    assert result["count"] == 0
    assert result["of"] == 1


def test_a_row_with_no_declared_cadence_is_excluded_from_the_denominator():
    """Nothing to check it against — §5.3: not counted as a pass or a fail."""
    unauditable = Entity(
        kind=Kind.COMPONENT, id="comp-w", state=State.HEALTHY,
        provenance=Provenance("registry", as_of=None),
    )
    result = staleness_honesty(_index_with(unauditable), now=NOW)
    assert result["of"] == 0
    assert result["count"] == 0


def test_an_artifact_disclosing_stale_as_its_own_raw_value_is_not_a_violation():
    art = Entity(
        kind=Kind.ARTIFACT, id="s3://b/k.json", state="stale",
        provenance=Provenance("object-store", as_of=(NOW - timedelta(hours=2)).isoformat()),
        detail={"cadence_minutes": 5},
    )
    result = staleness_honesty(_index_with(art), now=NOW)
    assert result["count"] == 0


def test_index_delegates_to_the_numbers_module():
    idx = Index()
    assert idx.staleness_honesty(now=NOW) == staleness_honesty(idx, now=NOW)


def test_a_declared_disabled_row_is_not_a_violation():
    """`lifecycle: disabled` is the STRONGEST disclosure of staleness the
    twelve-state vocabulary carries — the registry saying the component was
    not expected to report at all (`observability-policy.md` §8.3).

    Counting it as dishonest inverts the number: it becomes unclearable by
    honesty, since the only way to drop the count is to switch the component
    back on. `render/html.py::EXCEPTION_STATES` already excludes exactly these
    three declared states for the same reason.
    """
    for state in (State.DISABLED, State.DEPRECATED, State.RETIRED):
        declared_off = Entity(
            kind=Kind.COMPONENT, id=f"comp-{state.value.lower()}", state=state,
            provenance=Provenance("registry",
                                  as_of=(NOW - timedelta(days=8)).isoformat()),
            detail={"cadence_minutes": 60},
        )
        result = staleness_honesty(_index_with(declared_off), now=NOW)
        assert result["count"] == 0, state
        assert result["of"] == 1, state  # audited, and it passed


def test_a_stale_row_that_is_not_an_exception_state_is_still_caught():
    """The guard on the row above: widening the disclosed set must not blind
    §9.6 to DEGRADED/FAILED/HEALTHY, none of which say anything about age."""
    for state in (State.HEALTHY, State.DEGRADED, State.FAILED, State.NEVER_RAN):
        silent = Entity(
            kind=Kind.COMPONENT, id=f"comp-{state.value.lower()}", state=state,
            provenance=Provenance("checks",
                                  as_of=(NOW - timedelta(days=8)).isoformat()),
            detail={"cadence_minutes": 60},
        )
        result = staleness_honesty(_index_with(silent), now=NOW)
        assert result["count"] == 1, state
