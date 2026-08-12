"""`cloudwatch-metrics` — a metric namespace projected onto Component rows.

The defect this adapter closes: 87 of the console's 162 UNREPORTED components
(`alpha-engine-config-I7026`) are Lambda functions with a registry row and no
observation source of any kind. The console read Step Functions and check
envelopes and nothing else, so an entire substrate was registered, in service,
and emitting nothing into the surface.

The rules each test pins are the ones that make lowering that number honest
rather than cosmetic: a silent component is never green, an unread source is
never an absence claim, and a component the namespace has never heard of is a
finding rather than a blank.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from console.adapters import cloudwatch_metrics as cw
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State

NOW = datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_cache_between_tests():
    cw.reset_cache()
    yield
    cw.reset_cache()


def _config(**overrides):
    base = {
        "_name": "lambda-metrics",
        "region": "us-east-1",
        "namespace": "AWS/Lambda",
        "dimension": "FunctionName",
        "window_minutes": 1440,
        "history_days": 14,
        "cadence_seconds": 900,
        "discovery_facet": "substrate",
        "discovery_value": "lambda",
    }
    base.update(overrides)
    return base


def _fixture_reader(series: dict[tuple[str, str], list[tuple[str, float]]]):
    """Serve recorded points keyed by (dimension value, metric name)."""

    def reader(queries, start, end):
        return {
            q.query_id: list(series.get((q.dimension_value, q.metric_name), []))
            for q in queries
        }

    return reader


def _fetch(ids, series, **overrides):
    return cw.fetch(
        _config(**overrides),
        enumerator=lambda ns, metric, dim: list(ids),
        reader=_fixture_reader(series),
        now=NOW,
        clock=lambda: 0.0,
    )


def _state_of(result: AdapterResult, entity_id: str):
    return next(e for e in result.entities if e.id == entity_id).state


def _detail_of(result: AdapterResult, entity_id: str):
    return next(e for e in result.entities if e.id == entity_id).detail


# ------------------------------------------------------- the state table ----

def test_invocations_with_no_errors_is_healthy():
    result = _fetch(
        ["fn-busy"],
        {("fn-busy", "Invocations"): [("2026-08-12T17:00:00+00:00", 8.0)],
         ("fn-busy", "Errors"): []},
    )
    assert result.status is AdapterStatus.OK
    assert _state_of(result, "fn-busy") is State.HEALTHY


def test_any_error_in_the_window_is_failed_however_many_succeeded():
    """One error among eight invocations is still a component that failed. The
    counts stay on the row so the reader sees the ratio, but the STATE is the
    exception — a mostly-working component is not HEALTHY."""
    result = _fetch(
        ["fn-flaky"],
        {("fn-flaky", "Invocations"): [("2026-08-12T17:00:00+00:00", 8.0)],
         ("fn-flaky", "Errors"): [("2026-08-12T17:00:00+00:00", 1.0)]},
    )
    assert _state_of(result, "fn-flaky") is State.FAILED
    assert _detail_of(result, "fn-flaky")["invocations"] == 8.0


def test_zero_invocations_with_no_history_anywhere_is_never_ran():
    result = _fetch(
        ["fn-new"],
        {("fn-new", "Invocations"): [], ("fn-new", "Errors"): []},
    )
    assert _state_of(result, "fn-new") is State.NEVER_RAN


def test_a_silent_component_that_HAS_run_before_is_never_rendered_green():
    """The one that costs something, and the reason it is deliberate.

    Zero invocations in the window with history behind it is either idle by
    design or a missed trigger — DISABLED vs MISSED, the pair §8.3 exists to
    keep apart — and metrics alone cannot separate them. Only a declared
    cadence can, and §2.3 forbids this adapter from reading the registry that
    would carry one. So the honest render is UNREPORTED with the reason
    attached: loud, a finding, and still counted in the transparency gap.

    Rendering it HEALTHY would lower the gap by treating no-data as good news,
    which is the failure the whole number exists to detect."""
    result = cw.fetch(
        _config(),
        enumerator=lambda ns, metric, dim: ["fn-weekly"],
        reader=lambda queries, start, end: {
            q.query_id: ([("2026-08-06T09:00:00+00:00", 3.0)]
                         if q.query_id.startswith("h") else [])
            for q in queries
        },
        now=NOW,
        clock=lambda: 0.0,
    )
    entity = next(e for e in result.entities if e.id == "fn-weekly")
    assert entity.state is State.UNREPORTED
    assert entity.state is not State.HEALTHY
    assert entity.detail["last_invocation"] == "2026-08-06T09:00:00+00:00"
    assert "cadence" in entity.detail["unplaceable_reason"]


def test_an_unreadable_history_lookback_does_not_become_a_never_ran_claim():
    """NEVER_RAN says "no run in its history". Asserting it from a read that
    did not happen is the absence-of-evidence move §8.3 forbids, so the failed
    lookback is declared in `unavailable` and the row stays UNREPORTED."""
    calls = {"n": 0}

    def reader(queries, start, end):
        calls["n"] += 1
        if calls["n"] == 1:
            return {q.query_id: [] for q in queries}
        raise RuntimeError("throttled")

    result = cw.fetch(
        _config(),
        enumerator=lambda ns, metric, dim: ["fn-quiet"],
        reader=reader,
        now=NOW,
        clock=lambda: 0.0,
    )
    assert result.status is AdapterStatus.OK
    assert _state_of(result, "fn-quiet") is State.UNREPORTED
    assert "history" in result.unavailable


# --------------------------------------------------- discovery and absence --

def test_the_pass_is_a_scoped_discovery_claim():
    """DISCOVERY is what makes ABSENT computable for this substrate, and the
    scope is what stops that claim leaking onto substrates it never read."""
    result = _fetch(["fn-busy"], {("fn-busy", "Invocations"): [
        ("2026-08-12T17:00:00+00:00", 1.0)]})
    assert result.claim_class is ClaimClass.DISCOVERY
    assert result.discovery_scope == (("substrate", "lambda"),)


def test_a_registered_component_the_namespace_never_heard_of_renders_absent():
    """End to end through the index: the registry declares two Lambdas, the
    namespace enumerates one. The other is not a blank — it is ABSENT, the
    registry-expects-it/substrate-lacks-it finding."""
    registry = AdapterResult(
        name="registry", status=AdapterStatus.OK,
        claim_class=ClaimClass.DECLARATION,
        entities=(
            Entity(kind=Kind.COMPONENT, id="fn-busy", state=State.UNREPORTED,
                   provenance=Provenance(source="registry"),
                   facets={"substrate": "lambda"}),
            Entity(kind=Kind.COMPONENT, id="fn-gone", state=State.UNREPORTED,
                   provenance=Provenance(source="registry"),
                   facets={"substrate": "lambda"}),
        ),
    )
    observed = _fetch(["fn-busy"], {("fn-busy", "Invocations"): [
        ("2026-08-12T17:00:00+00:00", 4.0)]})

    index = Index()
    index.add_result(registry)
    index.add_result(observed)
    assert index.entity("fn-busy").state is State.HEALTHY
    assert index.entity("fn-gone").state is State.ABSENT


def test_the_dimension_value_is_the_component_id_verbatim():
    """§3.6 one-namespace. A slug minted here would double-render every
    registry row instead of merging with it."""
    result = _fetch(["alpha-engine-research-scanner"], {})
    assert [e.id for e in result.entities] == ["alpha-engine-research-scanner"]


def test_id_pattern_scopes_the_enumeration_without_a_code_change():
    result = _fetch(["keep-me", "drop-me"], {}, id_pattern="^keep-")
    assert [e.id for e in result.entities] == ["keep-me"]


# ---------------------------------------------------- failure is a state ----

def test_an_unenumerable_namespace_fails_loud_and_claims_no_absence():
    """A FAILED discovery pass must not satisfy the index's ABSENT guard —
    otherwise a throttled API call renders an entire substrate as missing."""
    result = cw.fetch(
        _config(),
        enumerator=_raise,
        reader=_fixture_reader({}),
        now=NOW,
        clock=lambda: 0.0,
    )
    assert result.status is AdapterStatus.FAILED
    assert result.unavailable == ("source",)
    assert result.entities == ()

    registry = AdapterResult(
        name="registry", status=AdapterStatus.OK,
        claim_class=ClaimClass.DECLARATION,
        entities=(Entity(kind=Kind.COMPONENT, id="fn-busy",
                         state=State.UNREPORTED,
                         provenance=Provenance(source="registry"),
                         facets={"substrate": "lambda"}),),
    )
    index = Index()
    index.add_result(registry)
    index.add_result(result)
    assert index.entity("fn-busy").state is State.UNREPORTED


def test_a_missing_boto3_is_a_declared_unavailability_not_zero_rows():
    result = cw.fetch(_config(), enumerator=None, reader=None,
                      now=NOW, clock=lambda: 0.0)
    if result.status is AdapterStatus.OK:
        pytest.skip("boto3 present in this environment")
    assert set(result.unavailable) <= {"enumerator", "reader"}
    assert result.entities == ()


# ------------------------------------------------------------------ cost ----

def test_the_source_is_read_on_its_own_cadence_not_the_index_rebuild():
    """GetMetricData bills per metric requested and the index rebuilds every
    60 seconds. Without this the adapter would cost 15x for data summarised
    over a 1440-minute window either way (`cost-management-policy.md`)."""
    reads = {"n": 0}

    def enumerator(ns, metric, dim):
        reads["n"] += 1
        return ["fn-busy"]

    clock = {"t": 0.0}
    for _ in range(5):
        cw.fetch(_config(), enumerator=enumerator,
                 reader=_fixture_reader({}), now=NOW, clock=lambda: clock["t"])
    assert reads["n"] == 1

    clock["t"] = 901.0
    cw.fetch(_config(), enumerator=enumerator, reader=_fixture_reader({}),
             now=NOW, clock=lambda: clock["t"])
    assert reads["n"] == 2


def test_the_declared_cadence_is_what_was_actually_read():
    """§5.9 bounds every row's freshness by the shortest declared source
    cadence. An adapter serving a cached read while declaring the index's
    60-second rebuild would report a freshness it does not have."""
    result = _fetch(["fn-busy"], {})
    assert result.declared_cadence_seconds == 900


def test_history_is_read_only_for_the_components_that_were_silent():
    """The second pass exists to tell NEVER_RAN from idle. Anything that
    invoked in the window has already answered that, and reading it anyway is
    billed metrics bought for nothing."""
    seen: list[list[str]] = []

    def reader(queries, start, end):
        seen.append([q.query_id for q in queries])
        return {
            q.query_id: ([("2026-08-12T17:00:00+00:00", 2.0)]
                         if q.dimension_value == "fn-busy"
                         and q.metric_name == "Invocations" else [])
            for q in queries
        }

    cw.fetch(_config(), enumerator=lambda *a: ["fn-busy", "fn-quiet"],
             reader=reader, now=NOW, clock=lambda: 0.0)
    history = [q for q in seen[1] if q.startswith("h")]
    assert len(history) == 1


def _raise(*args, **kwargs):
    raise RuntimeError("cloudwatch unreachable")
