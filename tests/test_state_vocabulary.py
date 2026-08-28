"""The state vocabulary is closed, total, and not ours to widen.

`observability-policy.md` §8.3 is normative for the fourteen states; this console
*renders* that vocabulary and does not define one. These tests are the
chokepoint for that, because the failure they guard against already happened:
the first implementation shipped a FORK — it added `UNKNOWN`, `NOT_MEASURED`
and `N/A-NOT-IMPL` (three of the four escape hatches §8.3 forbids by name) and
omitted `STALLED`, `MISSED`, `DISABLED`, `DEPRECATED` and `RETIRED`. Measured
before the fix, `State.UNKNOWN` was assigned at 14 call sites, more than any
other state: the total classifier's most common answer was its own
fall-through.

A fork is invisible in review because every individual `State.UNKNOWN` looks
careful — "declared, not guessed" was the comment on most of them. It is only
visible as a set comparison, which is what these tests are.
"""
from __future__ import annotations

import pytest

from console.model.entity import Entity, Provenance
from console.model.kinds import (
    COMPONENT_STATE_KINDS,
    DECLARED_LIFECYCLE_STATES,
    Kind,
    State,
)

#: observability-policy.md §8.3, transcribed. This literal is the point of the
#: test: it is a second, independent statement of the vocabulary, so a change to
#: the enum has to be made twice and the second time is against the policy text.
POLICY_FOURTEEN = {
    "HEALTHY",
    "RUNNING",
    "DEGRADED",
    "FAILED",
    "STALLED",
    "MISSED",
    "NEVER_RAN",
    "DISABLED",
    "DEPRECATED",
    "RETIRED",
    "ABSENT",
    "UNREGISTERED",
    "UNREPORTED",
    "ARMED",
}

#: §8.3 forbids these BY NAME: "UNKNOWN, OTHER, PENDING and N/A are all the
#: fall-through this vocabulary exists to remove."
FORBIDDEN_FALL_THROUGHS = {"UNKNOWN", "OTHER", "PENDING", "N/A", "NOT_MEASURED"}


def test_state_is_exactly_the_policy_fourteen():
    assert {s.value for s in State} == POLICY_FOURTEEN
    assert len(State) == 14


def test_no_fall_through_member_exists():
    names = {s.name for s in State} | {s.value for s in State}
    intersection = names & FORBIDDEN_FALL_THROUGHS
    assert intersection == set(), (
        f"{intersection} is a fall-through observability-policy.md §8.3 forbids "
        "by name. Where the classifier cannot place a component the answer is "
        "UNREPORTED — loud, and a finding."
    )
    # `N/A-NOT-IMPL` is a console-policy.md §11 carve-out for a §9 NUMBER a pane
    # does not yet compute. It is not a component state and may not appear here.
    assert not any("N/A" in s.value for s in State)


def test_the_informative_pairs_all_exist_and_are_distinct():
    """§8.3's content is in the pairs, and a fork always loses one side.

    DISABLED vs MISSED is a decision vs a defect; RETIRED vs ABSENT is a stated
    absence vs an unexplained one; NEVER_RAN vs MISSED is untested vs
    untriggered; UNREPORTED vs HEALTHY must never collapse; RUNNING vs STALLED
    is a current heartbeat vs an overdue one; RUNNING vs HEALTHY is unfinished
    vs ended.
    """
    for a, b in (
        (State.DISABLED, State.MISSED),
        (State.RETIRED, State.ABSENT),
        (State.NEVER_RAN, State.MISSED),
        (State.UNREPORTED, State.HEALTHY),
        (State.STALLED, State.FAILED),
        (State.DEGRADED, State.FAILED),
        (State.RUNNING, State.STALLED),
        (State.RUNNING, State.HEALTHY),
        (State.ARMED, State.HEALTHY),
        (State.ARMED, State.UNREPORTED),
    ):
        assert a is not b


def test_declared_lifecycle_states_are_the_three_and_only_the_three():
    """§8.3: DISABLED, DEPRECATED and RETIRED are declared in the registry,
    never inferred. Nothing reading telemetry may produce one."""
    assert set(DECLARED_LIFECYCLE_STATES.values()) == {
        State.DISABLED,
        State.DEPRECATED,
        State.RETIRED,
    }


def test_a_component_may_not_carry_a_raw_value_state():
    """The totality invariant, made structural (console-policy.md §5.1).

    The one route by which a fall-through could re-enter is an adapter reaching
    for a string because the enum had no comfortable member for its case. It
    cannot: construction fails.
    """
    for kind in COMPONENT_STATE_KINDS:
        with pytest.raises(ValueError, match="fourteen"):
            Entity(
                kind=kind,
                id="x",
                state="probably-fine",
                provenance=Provenance(source="test"),
            )


def test_non_component_kinds_may_carry_the_value_itself():
    """§5.1's second half: an Artifact is fresh or stale, an issue is open or
    closed. Neither is a component state, and forcing them into one is the
    pressure that produced the fork."""
    for kind, value in (
        (Kind.ARTIFACT, "stale"),
        (Kind.DECISION, "open"),
        (Kind.INCIDENT, "open-incident"),
        (Kind.SIGNAL, "no-baseline-declared"),
    ):
        ent = Entity(kind=kind, id="x", state=value, provenance=Provenance(source="t"))
        assert ent.state_value == value

    # …and they may still carry a §8.3 state where one genuinely applies.
    ent = Entity(
        kind=Kind.ARTIFACT, id="y", state=State.ABSENT,
        provenance=Provenance(source="t"),
    )
    assert ent.state_value == "ABSENT"


@pytest.mark.parametrize(
    "resolver, unrecognised_input",
    [
        ("checks_envelope", ("banana", None, None, 1.5)),
        ("state_machine", ("BANANA",)),
    ],
)
def test_no_classifier_has_a_default_branch_of_healthy(resolver, unrecognised_input):
    """§8.3's totality invariant: no default branch and no `else: HEALTHY`.

    An input outside a source's own vocabulary resolves to UNREPORTED — the
    loud answer — and never to the quiet one. This is the shape that lets a
    surface report a component it cannot classify as fine.
    """
    from datetime import datetime, timezone

    if resolver == "checks_envelope":
        from console.adapters.checks_envelope import _component_state

        status, ran_at, cadence, factor = unrecognised_input
        got = _component_state(
            status, ran_at, cadence, factor, datetime.now(tz=timezone.utc)
        )
    else:
        from console.adapters.state_machine import _run_state

        got = _run_state(*unrecognised_input)

    assert got is State.UNREPORTED, (
        f"{resolver} placed an unrecognised input as {got}. §8.3 has no "
        "default branch: the answer is UNREPORTED."
    )


def test_worst_of_an_unplaceable_set_is_unreported_not_healthy():
    from console.adapters.state_machine import _worst

    assert _worst([]) is State.UNREPORTED


def test_a_decision_already_taken_never_outranks_a_live_failure():
    """The severity order exists so a roll-up cannot report a DISABLED
    component as the worst thing happening while something is FAILED."""
    from console.adapters.state_machine import _worst

    assert _worst([State.DISABLED, State.FAILED]) is State.FAILED
    assert _worst([State.RETIRED, State.UNREPORTED]) is State.UNREPORTED
    # …and a declared decision still beats HEALTHY, so it stays visible.
    assert _worst([State.DISABLED, State.HEALTHY]) is State.DISABLED
