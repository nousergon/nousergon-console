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
from console.index.numbers import staleness_honesty, staleness_threshold_seconds
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus
from console.model.kinds import Kind, State

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


#: Every real source feeding §9.6 declares how often it is re-read, and since
#: alpha-engine-config-I7126 a row whose as-of source does NOT is excluded from
#: the denominator rather than audited against an unbounded observation lag. A
#: fixture with no declared cadence would therefore be testing the exclusion
#: branch in every test rather than the comparison. 60s is deliberately small
#: relative to the cadences under test so it never changes a verdict here; the
#: case where it does is `test_a_phase_sweep_never_changes_the_verdict`.
_SOURCE_CADENCE = 60.0


def _index_with(entity: Entity, source_cadence: float | None = _SOURCE_CADENCE) -> Index:
    idx = Index()
    idx.add_result(AdapterResult(name="fixture", status=AdapterStatus.OK,
                                 declared_cadence_seconds=source_cadence,
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
    """Nothing to check it against — §5.3: not counted as a pass or a fail.

    And with it the ONLY row, the aggregate refuses rather than rendering
    `0 of 0` (alpha-engine-config-I7126): an empty population reads exactly
    like an all-clear over a real one.
    """
    unauditable = Entity(
        kind=Kind.COMPONENT, id="comp-w", state=State.HEALTHY,
        provenance=Provenance("registry", as_of=None),
    )
    result = staleness_honesty(_index_with(unauditable), now=NOW)
    assert result["computable"] is False
    assert result["of"] is None
    assert result["count"] is None


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
    thirteen-state vocabulary carries — the registry saying the component was
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


def test_a_phase_sweep_never_changes_the_verdict():
    """The guard alpha-engine-config-I7126 exists for.

    A component whose declared cadence EQUALS its observing source's poll
    cadence — 15 minutes against a 900s adapter, the live shape of both
    `alpha-engine-router-exposure-probe-15min` and
    `alpha-engine-console-exposure-probe-15min` — is healthy at every phase
    offset, so §9.6 must return the same verdict at every phase offset. Before
    the fix the live number returned 1, 0, 0, 1, 0, 0, 1, 2, 2, 0 over 31
    minutes with nothing changing.

    Swept at one-minute steps across a FULL source period, twice over, so the
    sweep covers both "the source polled a moment ago" and "the source is
    about to poll". Removing the `source_cadence_seconds` term from
    `staleness_threshold_seconds` fails this at offsets past 22.5 minutes.
    """
    cadence_minutes, source_cadence = 15, 900.0
    verdicts = set()
    for fires_ago in range(0, cadence_minutes + 1):
        for polled_ago in range(0, int(source_cadence // 60) + 1):
            # The component fired `fires_ago` minutes back — inside its own
            # declared cadence, so it is behaving — and the source last looked
            # `polled_ago` minutes back. A poll that predates the fire has not
            # seen it, so the as-of the console holds is the PREVIOUS fire.
            last_seen = fires_ago if polled_ago <= fires_ago \
                else fires_ago + cadence_minutes
            healthy = Entity(
                kind=Kind.COMPONENT, id="probe-15min", state=State.HEALTHY,
                provenance=Provenance(
                    "cloudwatch:AWS/Events",
                    as_of=(NOW - timedelta(minutes=last_seen)).isoformat(),
                ),
                detail={"cadence_minutes": cadence_minutes},
            )
            result = staleness_honesty(
                _index_with(healthy, source_cadence=source_cadence), now=NOW)
            verdicts.add((result["count"], result["of"]))
    assert verdicts == {(0, 1)}, (
        "§9.6's verdict moved on the phase offset alone: " + repr(sorted(verdicts))
    )


def test_the_source_cadence_term_is_what_makes_the_sweep_stable():
    """Fails the sweep above with the term removed — so the guard guards.

    Asserted directly on `staleness_threshold_seconds` rather than by
    monkeypatching, so it states the property rather than the implementation:
    a 15-minute row observed through a 900s source must tolerate an age of a
    full cadence plus a full source period, and the one-term threshold does
    not.
    """
    worst_case_healthy_age = (15 + 15) * 60.0
    assert staleness_threshold_seconds(15 * 60.0, 900.0, 1.5) > worst_case_healthy_age
    assert staleness_threshold_seconds(15 * 60.0, 0.0, 1.5) < worst_case_healthy_age


def test_widening_never_lets_a_genuinely_stale_row_read_fresh():
    """The other half: the threshold moves by the OBSERVER's cadence and no
    more, so a component that stops reporting still crosses it — one observer
    period later than before, which is the floor a polling path can deliver."""
    dead = Entity(
        kind=Kind.COMPONENT, id="probe-dead", state=State.HEALTHY,
        provenance=Provenance("cloudwatch:AWS/Events",
                              as_of=(NOW - timedelta(minutes=46)).isoformat()),
        detail={"cadence_minutes": 15},
    )
    result = staleness_honesty(_index_with(dead, source_cadence=900.0), now=NOW)
    assert result["count"] == 1
    assert result["violations"] == ["probe-dead"]


def test_a_row_whose_as_of_source_declares_no_cadence_is_named_not_defaulted():
    """§5.3 / deliverable 2: excluded WITH a reason, never audited against an
    assumed-zero observation lag — and never silently dropped either."""
    named = Entity(
        kind=Kind.COMPONENT, id="comp-unbounded", state=State.HEALTHY,
        provenance=Provenance("mystery-source",
                              as_of=(NOW - timedelta(hours=9)).isoformat()),
        detail={"cadence_minutes": 5},
    )
    audited = Entity(
        kind=Kind.COMPONENT, id="comp-audited", state=State.HEALTHY,
        provenance=Provenance("checks", as_of=NOW.isoformat()),
        detail={"cadence_minutes": 5},
    )
    idx = Index()
    idx.add_result(AdapterResult(name="no-cadence", status=AdapterStatus.OK,
                                 entities=(named,)))
    idx.add_result(AdapterResult(name="fixture", status=AdapterStatus.OK,
                                 declared_cadence_seconds=_SOURCE_CADENCE,
                                 entities=(audited,)))
    result = staleness_honesty(idx, now=NOW)
    assert result["of"] == 1
    assert result["count"] == 0
    assert "comp-unbounded" in result["unauditable"]
    assert "mystery-source" in result["unauditable"]["comp-unbounded"]


def test_a_broken_declared_registry_makes_the_number_refuse():
    """Deliverable 3 — the `of: 0` tick. A registry that was declared and then
    failed removes rows from the denominator invisibly, so the aggregate
    refuses rather than reporting a count over what happened to load."""
    fresh = Entity(
        kind=Kind.COMPONENT, id="comp-ok", state=State.HEALTHY,
        provenance=Provenance("checks", as_of=NOW.isoformat()),
        detail={"cadence_minutes": 5},
    )
    idx = _index_with(fresh)
    idx.declare_registry("fleet")
    idx.record_registry_rows("fleet", count=0, ok=False)
    result = staleness_honesty(idx, now=NOW)
    assert result["computable"] is False
    assert "fleet" in str(result["reason"])
    assert result["of"] is None


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


def test_the_refusal_and_the_exclusions_both_reach_the_wire():
    """§5.3 is only obeyed if the reader sees it.

    Two shapes at the render boundary: a refusal must survive `_named_members`
    (which routes an ordinary result through `aggregate`, and `aggregate`
    raises on a `None` denominator), and an exclusion must be NAMED in the
    HTML rather than showing only as a denominator that quietly shrank
    (alpha-engine-config-I7126).
    """
    from console.render import json as render_json
    from console.render.html import _format_number

    refused = staleness_honesty(Index(), now=NOW)
    on_the_wire = render_json._named_members(refused, "violations")
    assert on_the_wire["computable"] is False
    assert on_the_wire["of"] is None
    assert "not computable" in _format_number(refused)

    named = Entity(
        kind=Kind.COMPONENT, id="comp-unbounded", state=State.HEALTHY,
        provenance=Provenance("mystery-source", as_of=NOW.isoformat()),
        detail={"cadence_minutes": 5},
    )
    audited = Entity(
        kind=Kind.COMPONENT, id="comp-audited", state=State.HEALTHY,
        provenance=Provenance("checks", as_of=NOW.isoformat()),
        detail={"cadence_minutes": 5},
    )
    idx = Index()
    idx.add_result(AdapterResult(name="no-cadence", status=AdapterStatus.OK,
                                 entities=(named,)))
    idx.add_result(AdapterResult(name="fixture", status=AdapterStatus.OK,
                                 declared_cadence_seconds=_SOURCE_CADENCE,
                                 entities=(audited,)))
    result = staleness_honesty(idx, now=NOW)
    assert render_json._named_members(result, "violations")["unauditable"]
    assert "comp-unbounded" in _format_number(result)


def test_an_inherited_alias_is_excluded_from_the_audited_population():
    """alpha-engine-config-I8973: an alias with no report of its own carries
    `detail.alias_state: "inherited"` (`index/graph.py::_apply_alias_
    inheritance`) — a pointer to an already-audited component, not a second
    row whose own staleness needs checking. Stale-looking on its own as-of
    (the parent's, copied across) but excluded from `checked` entirely,
    never counted as a pass."""
    inherited_alias = Entity(
        kind=Kind.COMPONENT, id="comp-old-name", state=State.HEALTHY,
        provenance=Provenance("checks", as_of=(NOW - timedelta(hours=1)).isoformat()),
        detail={"cadence_minutes": 5, "alias_of": "comp-parent",
                "alias_state": "inherited"},
    )
    result = staleness_honesty(_index_with(inherited_alias), now=NOW)
    assert result["computable"] is False  # no other row -> empty population
    assert "comp-old-name" not in result.get("violations", [])
