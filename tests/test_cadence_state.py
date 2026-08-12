"""§8.3 DISABLED vs MISSED, resolved at the merge (alpha-engine-config-I7060).

The behaviour under test is a comparison between two claims, so every case
below builds BOTH — a registry declaration carrying a cadence and a
substrate-counter claim carrying a silence — and asserts on the MERGED entity
as `graph._reconcile` produced it. A test that called `resolve_cadence_state`
on a hand-built entity would pass while the wiring was absent, and the wiring
is the thing that moves the number.

Timestamps are relative to the real clock rather than a frozen one: the code
path under test is reached through `Index.finalize()`, which takes no `now`,
and a test that had to patch `datetime` to exercise it would be testing a
seam that production never uses.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from console.index.cadence_state import resolve_cadence_state
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State

#: One nominal week and one nominal day, in minutes — the two cadences the
#: fleet's own registry rows carry after I7060.
WEEK = 10080
DAY = 1440


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ago(minutes: float) -> str:
    return (_now() - timedelta(minutes=minutes)).isoformat()


def _declaration(component_id: str, *, cadence=None, lifecycle_state=State.UNREPORTED):
    detail = {"registry_file": f"{component_id}.yaml", "produces": [], "consumes": []}
    if cadence is not None:
        detail["cadence_minutes"] = cadence
    return AdapterResult(
        claim_class=ClaimClass.DECLARATION,
        fetched_at=_now().isoformat(),
        name="registry",
        status=AdapterStatus.OK,
        entities=(Entity(
            kind=Kind.COMPONENT,
            id=component_id,
            state=lifecycle_state,
            provenance=Provenance(source="registry.d", as_of=None, evidence="file://x"),
            facets={"substrate": "lambda"},
            detail=detail,
        ),),
    )


def _silent_observation(component_id: str, *, last_invocation, invocations=0.0):
    """The shape `adapters/cloudwatch_metrics.py` emits for a silent component."""
    detail = {
        "invocations": invocations,
        "errors": 0.0,
        "window_minutes": DAY,
        "namespace": "AWS/Lambda",
        "dimension": "FunctionName",
    }
    if last_invocation is not None:
        detail["last_invocation"] = last_invocation
    return AdapterResult(
        claim_class=ClaimClass.DISCOVERY,
        fetched_at=_now().isoformat(),
        name="lambda-metrics",
        status=AdapterStatus.OK,
        discovery_scope=(("substrate", "lambda"),),
        entities=(Entity(
            kind=Kind.COMPONENT,
            id=component_id,
            state=State.UNREPORTED if invocations == 0.0 else State.HEALTHY,
            provenance=Provenance(
                source="cloudwatch:AWS/Lambda",
                as_of=last_invocation or _now().isoformat(),
                evidence="https://console.aws.amazon.com/cloudwatch",
            ),
            detail=detail,
        ),),
    )


def _merged(component_id, *results, staleness_factor=1.5):
    index = Index()
    index.set_staleness_factor(staleness_factor)
    for result in results:
        index.add_result(result)
    return index.finalize().entity(component_id)


# ---- the two sides of the comparison -----------------------------------------


def test_silence_beyond_the_declared_cadence_is_MISSED():
    """A weekly component silent for 15 days. WEEK * 1.5 = 15120 minutes."""
    cid = "alpha-engine-research-eval-judge-submit"
    ent = _merged(cid, _declaration(cid, cadence=WEEK),
                  _silent_observation(cid, last_invocation=_ago(21600)))
    assert ent.state is State.MISSED


def test_silence_inside_the_declared_cadence_is_HEALTHY():
    """Two days silent under a weekly cadence is the NORMAL case, not a gap.

    This is the assertion that must never become "widen the window": the
    evidence is a real invocation two days ago, read from the substrate's own
    history, against a declared expectation of one run a week. Widening the
    adapter's window to reach the same verdict would paint every silent
    component green regardless of what any of them was supposed to do.
    """
    cid = "alpha-engine-research-eval-judge-submit"
    ent = _merged(cid, _declaration(cid, cadence=WEEK),
                  _silent_observation(cid, last_invocation=_ago(2884)))
    assert ent.state is State.HEALTHY


# ---- everything that must stay UNREPORTED ------------------------------------


def test_no_declared_cadence_stays_UNREPORTED():
    """The pre-I7060 state, and still the honest one for an undeclared row."""
    cid = "alpha-engine-crypto-balances"
    ent = _merged(cid, _declaration(cid),
                  _silent_observation(cid, last_invocation=_ago(8644)))
    assert ent.state is State.UNREPORTED


def test_no_last_invocation_stays_UNREPORTED_and_is_never_green():
    """Silence with NOTHING behind it is not evidence of health at any cadence.

    `principles.md` §2.7: a component whose substrate could not say when it
    last ran has produced no data, and no data is never rendered green however
    generous its declared cadence.
    """
    cid = "alpha-engine-groom-inject-mock"
    ent = _merged(cid, _declaration(cid, cadence=WEEK),
                  _silent_observation(cid, last_invocation=None))
    assert ent.state is State.UNREPORTED


def test_a_declaration_alone_never_flips_to_HEALTHY():
    """No source read a window, so there is no silence to compare against.

    Without the `invocations`/`window_minutes` guard a registry file carrying a
    cadence and nothing else would render green — a declaration beating an
    absent observation, which inverts §2.5's state precedence entirely.
    """
    cid = "alpha-engine-crypto-balances"
    ent = _merged(cid, _declaration(cid, cadence=15))
    assert ent.state is State.UNREPORTED


@pytest.mark.parametrize("cadence", [0, -5, "", "weekly", None])
def test_an_unusable_cadence_is_not_a_cadence(cadence):
    cid = "alpha-engine-crypto-balances"
    ent = _merged(cid, _declaration(cid, cadence=cadence),
                  _silent_observation(cid, last_invocation=_ago(99999)))
    assert ent.state is State.UNREPORTED


# ---- states this may never touch ---------------------------------------------


def test_a_declared_DISABLED_row_is_untouched():
    """§8.3: a decision may not be re-rendered as a defect by a cadence sum."""
    cid = "alpha-engine-scheduled-groom-dispatcher"
    ent = _merged(cid,
                  _declaration(cid, cadence=480, lifecycle_state=State.DISABLED),
                  _silent_observation(cid, last_invocation=_ago(8644)))
    assert ent.state is State.DISABLED


def test_an_invoked_component_is_untouched():
    cid = "alpha-engine-eod-backstop"
    ent = _merged(cid, _declaration(cid, cadence=DAY),
                  _silent_observation(cid, last_invocation=_ago(60), invocations=3.0))
    assert ent.state is State.HEALTHY


def test_the_configured_staleness_factor_is_the_one_applied():
    """The same 2884-minute silence, judged against a DAILY cadence, is MISSED."""
    cid = "alpha-engine-research-signals-envelope"
    ent = _merged(cid, _declaration(cid, cadence=DAY),
                  _silent_observation(cid, last_invocation=_ago(2884)))
    assert ent.state is State.MISSED


# ---- the invariant that keeps this from breaking §9.6 -------------------------


def test_staleness_honesty_gains_the_declared_rows_without_gaining_violations():
    """§9.6's denominator rises; its violation count does not (I7020).

    Declaring cadences is what finally gives `staleness_honesty` real members —
    and a stale one renders MISSED, which §9.6 already treats as a row that has
    disclosed its own age. This is the check that "declare more cadences" is
    not a way to move clause 2 by breaking clause 4.
    """
    index = Index()
    index.set_staleness_factor(1.5)
    index.add_result(_declaration("fresh", cadence=WEEK))
    index.add_result(_silent_observation("fresh", last_invocation=_ago(2884)))
    index.add_result(_declaration("stale", cadence=WEEK))
    index.add_result(_silent_observation("stale", last_invocation=_ago(21600)))
    index.finalize()

    assert index.entity("stale").state is State.MISSED
    numbers = index.staleness_honesty()
    assert numbers["of"] == 2
    assert numbers["count"] == 0, numbers["violations"]


# ---- the unit, called directly ------------------------------------------------


def test_resolve_is_a_no_op_on_a_non_component():
    ent = Entity(
        kind=Kind.ARTIFACT,
        id="s3://bucket/key.json",
        state="absent",
        provenance=Provenance(source="s3", as_of=None, evidence="s3://bucket/key.json"),
        detail={"invocations": 0.0, "window_minutes": DAY, "cadence_minutes": WEEK,
                "last_invocation": _ago(99999)},
    )
    assert resolve_cadence_state(ent).state == "absent"
