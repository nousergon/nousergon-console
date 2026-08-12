"""object-store adapter tests — recorded key lists, no live bucket."""
from __future__ import annotations

from datetime import datetime, timezone

from console.adapters import object_store
from console.model.envelope import AdapterStatus
from console.model.kinds import Kind, State

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
PATTERN = r"ops/checks/(?P<component_id>[^/]+)/latest.json"


def _lister(bucket, prefix):
    return [
        ("ops/checks/comp-alpha/latest.json", "2026-07-31T11:30:00Z"),  # fresh
        ("ops/checks/comp-beta/latest.json", "2026-07-30T00:00:00Z"),   # stale
    ]


def _cfg():
    return {
        "bucket": "fixture-bucket",
        "prefix": "ops/checks/",
        "key_pattern": PATTERN,
        "cadence": "1h",
        "staleness_factor": 1.5,
    }


def test_keys_become_artifacts_keyed_by_key():
    result = object_store.fetch(_cfg(), lister=_lister, now=NOW)
    assert result.status is AdapterStatus.OK
    ids = {e.id for e in result.entities}
    assert "ops/checks/comp-alpha/latest.json" in ids
    assert all(e.kind is Kind.ARTIFACT for e in result.entities)


def test_fresh_vs_stale_rendered_from_cadence():
    result = object_store.fetch(_cfg(), lister=_lister, now=NOW)
    by_id = {e.id: e for e in result.entities}
    # An Artifact does not resolve to a component state, so §5.1's second
    # half applies and the row carries the value itself.
    assert by_id["ops/checks/comp-alpha/latest.json"].state == "fresh"
    assert by_id["ops/checks/comp-beta/latest.json"].state == "stale"


def test_component_edge_derived_from_named_group():
    result = object_store.fetch(_cfg(), lister=_lister, now=NOW)
    rels = {(e.source, e.rel, e.target) for e in result.edges}
    assert ("comp-alpha", "produces", "ops/checks/comp-alpha/latest.json") in rels


def test_the_three_not_computable_cases_stay_three_facts():
    def lister(b, p):
        return [("ops/checks/comp-x/latest.json", None)]
    result = object_store.fetch(_cfg(), lister=lister, now=NOW)
    # No stamp, no declared cadence and an unparseable stamp are three
    # different findings with three different fixes (§5.5). Collapsing them
    # into one token loses the fix.
    assert result.entities[0].state == "no-freshness-stamp"


def test_lister_failure_is_failed_not_empty():
    def boom(b, p):
        raise RuntimeError("bucket unreachable")
    result = object_store.fetch(_cfg(), lister=boom, now=NOW)
    assert result.status is AdapterStatus.FAILED


def test_unmatched_keys_skipped():
    def lister(b, p):
        return [("other/path/file.txt", "2026-07-31T11:00:00Z")]
    result = object_store.fetch(_cfg(), lister=lister, now=NOW)
    assert result.entities == ()


def test_consumer_edge_derived_from_named_group():
    """nousergon-console#52: a consumer_id named group derives the
    consumed-by edge, symmetric with component_id -> produces. Relation-
    reachable in both directions per §3.1/§3.3 — the reverse ("consumes") is
    derived by the index, not asserted here."""
    pattern = r"ops/checks/(?P<component_id>[^/]+)/(?P<consumer_id>[^/]+)/latest.json"

    def lister(bucket, prefix):
        return [("ops/checks/comp-alpha/comp-dashboard/latest.json", "2026-07-31T11:30:00Z")]

    cfg = {**_cfg(), "key_pattern": pattern}
    result = object_store.fetch(cfg, lister=lister, now=NOW)
    rels = {(e.source, e.rel, e.target) for e in result.edges}
    assert ("ops/checks/comp-alpha/comp-dashboard/latest.json", "consumed-by",
            "comp-dashboard") in rels


def test_no_consumer_edge_when_pattern_names_no_consumer_id():
    # The default fixture pattern names no consumer_id group — no
    # consumed-by edge is invented where the config declared none.
    result = object_store.fetch(_cfg(), lister=_lister, now=NOW)
    assert not any(e.rel == "consumed-by" for e in result.edges)


# --------------------------------------------- declared question (nousergon-console#61) --

def test_declared_question_renders_as_synthetic_text_field():
    """A source with no body to read (a markdown briefing, a parquet dump)
    still gets its declared question on the surface — via the same synthetic
    declared-field convention `s3-records` uses for the same config key
    (§4.4), so no new rendering code is needed either place."""
    cfg = {**_cfg(), "question": "What did the research morning-briefing email say?"}
    result = object_store.fetch(cfg, lister=_lister, now=NOW)
    for ent in result.entities:
        q = ent.detail["fields"]["question"]
        assert q == {"value": "What did the research morning-briefing email say?", "render": "text"}


def test_no_question_declared_leaves_fields_absent():
    result = object_store.fetch(_cfg(), lister=_lister, now=NOW)
    assert all("fields" not in e.detail for e in result.entities)


# ------------------------------------------ staleness honesty (I7050) ------


def test_a_declared_cadence_is_exposed_as_cadence_minutes():
    """`_state()` already re-derives fresh/stale from the same cadence — this
    is what lets `staleness_honesty()` ALSO independently re-derive it,
    rather than trusting the adapter's own verdict (§9.6's whole point,
    previously unreachable for every object-store-sourced artifact)."""
    result = object_store.fetch(_cfg(), lister=_lister, now=NOW)
    for ent in result.entities:
        assert ent.detail["cadence_minutes"] == 60.0  # "1h"


def test_no_declared_cadence_means_no_cadence_minutes():
    cfg = {k: v for k, v in _cfg().items() if k != "cadence"}
    result = object_store.fetch(cfg, lister=_lister, now=NOW)
    assert all("cadence_minutes" not in e.detail for e in result.entities)
