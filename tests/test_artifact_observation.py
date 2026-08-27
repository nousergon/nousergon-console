"""A declaration may not assert an absence — and the coverage number that keeps
the honest answer visible (alpha-engine-config-I8765).

The defect these guard against, measured on the live surface 2026-08-27: 177 of
508 exception rows were artifacts a `declared-registry` had defaulted to
`absent` while no observation half was wired anywhere. `absent` means *the
substrate does not have it*, which `observability-policy.md` §8.3 permits only
off a successful discovery pass — so the surface was rendering
absence-of-evidence as evidence-of-absence, 177 times, from one config value.

Four clauses, one per test group:

1. A declared-only row renders `unobserved`, which is NOT an exception.
2. Configuring an exception state as a declared default fails the BUILD, and
   the message names the fragment to edit.
3. The observation half reads real keys — HEAD per key, template partitions
   resolved from the declared cadence, and NOTHING looked at that cannot be
   honestly resolved.
4. The coverage number states how many declared rows anything looked at, with
   its denominator inline (§5.3) and its members linked (§5.1).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import yaml

from console.adapters import declared_registry, object_store
from console.config import ConfigError, build_index, validate_config
from console.index.graph import Index
from console.index.numbers import artifact_observation_coverage, staleness_honesty
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import EXCEPTION_VALUES, UNOBSERVED_VALUE, Kind
from console.render.html import is_exception, observation_coverage_line
from console.render.json import numbers as json_numbers

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def _weekday_checker(day: str) -> bool:
    """Mon-Fri is a trading day. Injected so no test needs a calendar library."""
    return datetime.fromisoformat(day).weekday() < 5


def _registry_doc(tmp_path, entries):
    path = tmp_path / "artifact-registry.yaml"
    path.write_text(yaml.safe_dump({"artifacts": entries}))
    return str(path)


# ------------------------------------------ 1. declared-only is unobserved --

def test_declared_only_row_is_unobserved_not_absent(tmp_path):
    path = _registry_doc(tmp_path, [
        {"key": "data/x.json", "cadence": "saturday_sf"},
    ])
    result = declared_registry.fetch(
        {"path": path, "kind": "artifact", "id_field": "key",
         "entries_field": "artifacts"}, now=NOW)
    (ent,) = result.entities
    assert ent.state == UNOBSERVED_VALUE
    assert ent.state != "absent"


def test_unobserved_is_not_an_exception_and_absent_still_is():
    assert UNOBSERVED_VALUE not in EXCEPTION_VALUES
    assert "absent" in EXCEPTION_VALUES
    declared = Entity(kind=Kind.ARTIFACT, id="a", state=UNOBSERVED_VALUE,
                      provenance=Provenance("reg", None, "file://reg"))
    observed = Entity(kind=Kind.ARTIFACT, id="b", state="absent",
                      provenance=Provenance("s3://b/b", None, "s3://b/b"))
    assert not is_exception(declared)
    assert is_exception(observed)


def test_unobserved_is_disclosed_to_staleness_honesty():
    """§9.6 must not read "nothing looked at me" as a surface lying about age —
    that reading is unclearable by honesty, since the only way down is to hide
    the row."""
    index = Index()
    ent = Entity(
        kind=Kind.ARTIFACT, id="data/x.json", state=UNOBSERVED_VALUE,
        provenance=Provenance("registry", "2020-01-01T00:00:00Z", "file://r"),
        detail={"cadence_minutes": 60},
    )
    index.add_result(AdapterResult(
        claim_class=ClaimClass.DECLARATION, fetched_at="2026-08-27T12:00:00Z",
        name="registry", status=AdapterStatus.OK, entities=(ent,),
        declared_cadence_seconds=60,
    ))
    out = staleness_honesty(index.finalize(), now=NOW)
    assert out["of"] == 1          # audited, not excluded — the row IS checked
    assert out["violations"] == []


# ------------------------------------------------ 2. the build-time refusal --

@pytest.mark.parametrize("value", sorted(EXCEPTION_VALUES))
def test_exception_valued_default_state_fails_the_build(value):
    config = {"adapters": [{
        "name": "artifact-registry", "kind": "declared-registry",
        "enabled": True, "config": {"default_state": value},
    }]}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    # The message must name the FRAGMENT: the fix is a one-line edit in a file
    # the operator has to be able to find from the error alone.
    assert "artifact-registry" in str(excinfo.value)
    assert value in str(excinfo.value)


def test_registry_shaped_entry_is_guarded_too():
    """A declared-registry configured under `registry:` carries its adapter
    config at the top level rather than under `config:` — one guard, both
    spellings, or the rule is evadable by moving the block."""
    config = {"registry": {"name": "fleet-artifacts", "adapter": "declared-registry",
                           "default_state": "absent"}}
    with pytest.raises(ConfigError, match="fleet-artifacts"):
        validate_config(config)


def test_unobserved_default_is_accepted_and_no_default_is_accepted():
    validate_config({"adapters": [
        {"name": "a", "kind": "declared-registry",
         "config": {"default_state": UNOBSERVED_VALUE}},
        {"name": "b", "kind": "declared-registry", "config": {}},
    ]})


def test_build_index_refuses_before_any_adapter_runs():
    with pytest.raises(ConfigError):
        build_index({"adapters": [{
            "name": "artifact-registry", "kind": "declared-registry",
            "enabled": True, "config": {"default_state": "absent"}}]})


# --------------------------------------------- 3. the observation half ------

def _keys_fetch(entries, store, **config):
    return object_store.fetch(
        {"bucket": "bkt", "keys": entries, **config},
        stat=lambda uri: store.get(uri),
        now=NOW,
        trading_day_checker=_weekday_checker,
    )


def test_literal_key_present_renders_fresh_and_absent_when_missing():
    result = _keys_fetch(
        [{"key": "a/latest.json", "cadence": "eod_sf"},
         {"key": "b/latest.json", "cadence": "eod_sf"}],
        {"s3://bkt/a/latest.json": "2026-08-27T06:00:00+00:00"},
    )
    states = {e.id: e.state for e in result.entities}
    assert states == {"a/latest.json": "fresh", "b/latest.json": "absent"}


def test_absent_here_is_an_observation_not_a_default():
    """The whole point: this `absent` came off a HEAD that returned nothing,
    and the row names the object store as its source."""
    result = _keys_fetch([{"key": "b/latest.json", "cadence": "eod_sf"}], {})
    (ent,) = result.entities
    assert result.claim_class is ClaimClass.OBSERVATION
    assert ent.state == "absent"
    assert ent.provenance.source == "s3://bkt/b/latest.json"


def test_a_partitioned_key_resolves_to_the_last_expected_partition():
    """A `saturday_sf` pipeline publishing the previous trading day: as of
    Thursday 2026-08-27 the last run was Saturday the 22nd, which wrote
    Friday the 21st. Measured against the live bucket before it was declared —
    `market_data/weekly/2026-08-22/` does not exist at all."""
    result = _keys_fetch(
        [{"key": "signals/{trading_day}/signals.json", "cadence": "saturday_sf",
          "partition": "last-trading-day-before-run", "sla_minutes_after_cron": 60}],
        {"s3://bkt/signals/2026-08-21/signals.json": "2026-08-22T11:03:00+00:00"},
    )
    (ent,) = result.entities
    # The ID is the key AS DECLARED — §3.2, and what makes the merge work.
    assert ent.id == "signals/{trading_day}/signals.json"
    assert ent.detail["resolved_key"] == "signals/2026-08-21/signals.json"
    assert ent.provenance.evidence == "s3://bkt/signals/2026-08-21/signals.json"
    assert ent.state == "fresh"


def test_run_date_and_last_trading_day_are_different_resolvers():
    """Both spellings occur in one real registry, so the mapping is declared
    per key and never inferred from the cadence."""
    store = {"s3://bkt/x/2026-08-22.json": "2026-08-22T16:00:00+00:00",
             "s3://bkt/y/2026-08-21.json": "2026-08-22T16:00:00+00:00"}
    result = _keys_fetch(
        [{"key": "x/{date}.json", "cadence": "saturday_sf", "partition": "run-date",
          "sla_minutes_after_cron": 60},
         {"key": "y/{date}.json", "cadence": "saturday_sf",
          "partition": "last-trading-day-before-run", "sla_minutes_after_cron": 60}],
        store,
    )
    assert {e.detail["resolved_key"] for e in result.entities} == {
        "x/2026-08-22.json", "y/2026-08-21.json"}


@pytest.mark.parametrize("entry", [
    {"key": "g/{date}/thing.json", "cadence": "continuous", "interval_minutes": 60},
    {"key": "g/{date}/thing.json", "cadence": "event_driven"},
    {"key": "g/{ticker}.json", "cadence": "saturday_sf"},
])
def test_an_unresolvable_partition_is_not_looked_at(entry):
    """No claim, so the declaration's `unobserved` stands. HEADing a key the
    source never writes would render the 404 as a finding — the exact defect
    this slice removes, reintroduced one layer down."""
    result = _keys_fetch([entry], {})
    assert result.entities == ()
    assert result.unavailable == ("unresolved-partition:1",)


def test_a_source_that_raises_is_reported_never_rendered_as_absence():
    def boom(uri):
        raise RuntimeError("throttled")

    result = object_store.fetch(
        {"bucket": "bkt", "keys": [{"key": "a.json", "cadence": "eod_sf"}]},
        stat=boom, now=NOW, trading_day_checker=_weekday_checker)
    assert result.entities == ()
    assert result.unavailable == ("unreadable-keys:1",)


def test_keys_mode_never_lists():
    """A prefix listing over this registry's dated partitions is the cost class
    the refresh budget cannot absorb — the reason this mode exists."""
    def lister(bucket, prefix):  # pragma: no cover - must never be called
        raise AssertionError("keys mode must not list a prefix")

    result = object_store.fetch(
        {"bucket": "bkt", "keys": [{"key": "a.json", "cadence": "eod_sf"}]},
        lister=lister, stat=lambda uri: None, now=NOW,
        trading_day_checker=_weekday_checker)
    assert result.status is AdapterStatus.OK


# ------------------------------- 4. the merge, and the coverage number ------

def _index_with(declared_entries, observed_entries, observation_ok=True):
    index = Index()
    index.add_result(AdapterResult(
        claim_class=ClaimClass.DECLARATION, fetched_at="2026-08-27T12:00:00Z",
        name="artifact-registry", status=AdapterStatus.OK,
        entities=tuple(declared_entries)))
    index.add_result(AdapterResult(
        claim_class=ClaimClass.OBSERVATION, fetched_at="2026-08-27T12:00:00Z",
        name="artifact-observation",
        status=AdapterStatus.OK if observation_ok else AdapterStatus.FAILED,
        entities=tuple(observed_entries)))
    return index.finalize()


def _declared(key):
    return Entity(kind=Kind.ARTIFACT, id=key, state=UNOBSERVED_VALUE,
                  provenance=Provenance("/registry/ARTIFACT_REGISTRY.yaml", None,
                                        "file:///registry/ARTIFACT_REGISTRY.yaml"))


def _observed(key, state, as_of=None):
    return Entity(kind=Kind.ARTIFACT, id=key, state=state,
                  provenance=Provenance(f"s3://bkt/{key}", as_of, f"s3://bkt/{key}"))


def test_an_observed_absence_names_the_reader_that_looked():
    """I8765's closes-when: no `absent` row may cite the registry document as
    its source. An absence has no as-of on either claim, and the row used to
    fall back to the best-RANKED claim — the declaration — so it read
    "ARTIFACT_REGISTRY.yaml says this is missing", which no registry can know.
    """
    index = _index_with([_declared("a.json")], [_observed("a.json", "absent")])
    ent = index.entity("a.json")
    assert ent.state == "absent"
    assert ent.provenance.source == "s3://bkt/a.json"


def test_an_observation_wins_the_state_over_the_declared_default():
    index = _index_with([_declared("a.json")],
                        [_observed("a.json", "fresh", "2026-08-27T06:00:00Z")])
    assert index.entity("a.json").state == "fresh"


def test_coverage_counts_declared_rows_that_were_looked_at():
    index = _index_with([_declared("a.json"), _declared("b.json"), _declared("c.json")],
                        [_observed("a.json", "fresh", "2026-08-27T06:00:00Z")])
    cov = artifact_observation_coverage(index)
    assert cov["observed"] == 1
    assert cov["of"] == 3            # the DECLARED population, never narrowed
    assert cov["unobserved_ids"] == ["b.json", "c.json"]


def test_an_unreachable_observation_is_not_coverage():
    """A source declaring its own blindness is not a reading of the fleet."""
    index = _index_with([_declared("a.json")], [_observed("a.json", "fresh")],
                        observation_ok=False)
    assert artifact_observation_coverage(index)["observed"] == 0


def test_coverage_refuses_an_empty_population():
    cov = artifact_observation_coverage(Index().finalize())
    assert cov["computable"] is False
    assert cov["count"] is None


def test_the_number_is_published_in_json_and_on_the_page():
    index = _index_with([_declared("a.json"), _declared("b.json")],
                        [_observed("a.json", "fresh", "2026-08-27T06:00:00Z")])
    from console.render.html import is_exception as _is_exc

    exceptions = [e for e in index.all() if _is_exc(e)]
    n = json_numbers(index, exceptions, index.conflicts(), index.transparency_gap())
    assert n["artifact_observation_coverage"]["count"] == 1
    assert n["artifact_observation_coverage"]["of"] == 2
    assert n["artifact_observation_coverage"]["unobserved_ids"] == ["b.json"]

    line = observation_coverage_line(index)
    assert "1 / 2" in line
    # §5.1's evidence field: the gap is navigable, not merely counted.
    assert f"state={UNOBSERVED_VALUE}" in line or UNOBSERVED_VALUE in line
    assert "b.json</a>" in line
