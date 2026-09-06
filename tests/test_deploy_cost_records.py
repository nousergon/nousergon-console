"""The per-deploy cost surface (alpha-engine-config-I10075).

Two `s3-records` bindings, not a new adapter: `docs/adapters.md`'s boundary
test stops at step 2, because both prefixes are the shape `s3-records` already
reads and differ only in which bucket and prefix they point at. So what needs
testing is not adapter code — it is that these two CONFIGURATIONS, driven over
records shaped exactly as the producer writes them, render the four things the
issue asks the console to answer: per-repo deploys/day, minutes/day (estimate
vs billed), CodeBuild $/day, and the red-after-merge count.

Fixtures are synthetic (this repo is public; no fleet topology) but their
FIELD NAMES are the producer's, so a rename on either side fails here.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from console.adapters import s3_records
from console.model.envelope import AdapterStatus
from console.model.kinds import Kind, State

NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
BUCKET = "fixture-bucket"
_CONFIG_EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


def _example(name: str) -> dict:
    doc = yaml.safe_load(_CONFIG_EXAMPLE.read_text())
    for block in doc["adapters"]:
        if block["name"] == name:
            return dict(block["config"])
    raise AssertionError(f"config.example.yaml declares no {name!r} block")


def _by_id(result):
    return {e.id: e for e in result.entities}


# ---------------------------------------------------------------------------
# deploy-cost — one Run per push-to-main merge
# ---------------------------------------------------------------------------

CLEAN_DEPLOY = {
    "schema_version": "1.0.0",
    "record_type": "deploy_cost",
    "event_id": "20260906T120000Z_alpha_aaa111",
    "entry_key": "changelog/entries/2026-09-06/20260906T120000Z_alpha_aaa111.json",
    "repo": "acme/alpha",
    "sha": "a" * 40,
    "ts_utc": "2026-09-06T12:00:00Z",
    "day": "2026-09-06",
    "pr_number": 4242,
    "ci_jobs": 28,
    "ci_minutes_billable_est": 28,
    "runner_mode": "gha",
    "codebuild_usd": 0.0,
    "checks_failed": 0,
    "aws_writes": ["s3://fixture-bucket/changelog/entries/{date}/{event_id}.json"],
    "deploy_state": "clean",
}

RED_DEPLOY = {
    **CLEAN_DEPLOY,
    "event_id": "20260906T180000Z_alpha_bbb222",
    "sha": "b" * 40,
    "ts_utc": "2026-09-06T18:00:00Z",
    "ci_jobs": 4,
    "ci_minutes_billable_est": 9,
    "runner_mode": "codebuild",
    "codebuild_usd": 0.045,
    "checks_failed": 2,
    "deploy_state": "red_after_merge",
}

OTHER_REPO_DEPLOY = {
    **CLEAN_DEPLOY,
    "event_id": "20260906T090000Z_beta_ccc333",
    "repo": "acme/beta",
    "ci_jobs": 2,
    "ci_minutes_billable_est": 2,
    "ts_utc": "2026-09-06T09:00:00Z",
}

_COST_KEYS = {
    f"changelog/deploy_cost/2026-09-06/{r['event_id']}.json": r
    for r in (CLEAN_DEPLOY, RED_DEPLOY, OTHER_REPO_DEPLOY)
}


def _cost_lister(bucket, prefix):
    return [(k, "2026-09-06T23:00:00+00:00") for k in sorted(_COST_KEYS)]


def _cost_reader(bucket, key):
    return _COST_KEYS[key]


def _fetch_cost():
    return s3_records.fetch(
        {**_example("deploy-cost"), "bucket": BUCKET},
        lister=_cost_lister, reader=_cost_reader, now=NOW)


def test_one_run_entity_per_deploy_keyed_by_the_changelog_event_id():
    """The id is the changelog entry's own `event_id`, so the cost claim and
    `changelog-events`' claim merge onto ONE entity rather than two rows
    describing the same deploy (console-policy.md §2.5)."""
    result = _fetch_cost()
    assert result.status is AdapterStatus.OK
    assert set(_by_id(result)) == {
        "20260906T120000Z_alpha_aaa111",
        "20260906T180000Z_alpha_bbb222",
        "20260906T090000Z_beta_ccc333",
    }
    assert all(e.kind is Kind.RUN for e in result.entities)


def test_a_clean_deploy_is_healthy_and_a_red_after_merge_is_degraded():
    entities = _by_id(_fetch_cost())
    assert entities["20260906T120000Z_alpha_aaa111"].state is State.HEALTHY
    assert entities["20260906T180000Z_alpha_bbb222"].state is State.DEGRADED


def test_state_comes_from_the_producers_declared_vocabulary_not_a_number():
    """`checks_failed > 0` is a number the console must not interpret on its
    own. The producer declares `deploy_state` and the `state_map` translates
    it — the §8.3 rule that a disposition is declared, never inferred."""
    cfg = _example("deploy-cost")
    assert cfg["state_field"] == "deploy_state"
    assert cfg["state_map"] == {"clean": "HEALTHY", "red_after_merge": "DEGRADED"}


def test_minutes_jobs_dollars_and_red_count_all_render_as_declared_fields():
    entity = _by_id(_fetch_cost())["20260906T180000Z_alpha_bbb222"]
    fields = entity.detail["fields"]
    assert fields["ci_jobs"]["value"] == 4
    assert fields["ci_minutes_billable_est"]["value"] == 9
    assert fields["codebuild_usd"]["value"] == 0.045
    assert fields["codebuild_usd"]["unit"] == "usd"
    assert fields["checks_failed"]["value"] == 2
    assert fields["runner_mode"]["value"] == "codebuild"


def test_aws_writes_reach_the_row_so_a_deploy_says_what_it_touched():
    entity = _by_id(_fetch_cost())["20260906T120000Z_alpha_aaa111"]
    assert entity.detail["fields"]["aws_writes"]["value"] == CLEAN_DEPLOY["aws_writes"]


def test_repo_and_runner_mode_are_facets_so_per_repo_per_day_is_a_walk():
    """"Per-repo deploys/day" must be a traversal of the entity graph, not a
    pane that only exists because someone drew it (console-policy.md §3.1)."""
    entities = _by_id(_fetch_cost())
    alpha = entities["20260906T120000Z_alpha_aaa111"]
    beta = entities["20260906T090000Z_beta_ccc333"]
    assert alpha.facets["repo"] == "acme/alpha"
    assert beta.facets["repo"] == "acme/beta"
    assert alpha.facets["day"] == "2026-09-06"
    assert entities["20260906T180000Z_alpha_bbb222"].facets["runner_mode"] == "codebuild"


def test_as_of_is_the_deploys_own_timestamp_not_the_objects_last_modified():
    entity = _by_id(_fetch_cost())["20260906T120000Z_alpha_aaa111"]
    assert entity.provenance.as_of == "2026-09-06T12:00:00Z"


def test_the_binding_declares_its_own_read_cadence():
    """An adapter that omits `declared_cadence_seconds` makes its rows
    unauditable (§9.6) — they leave the staleness denominator silently."""
    assert _fetch_cost().declared_cadence_seconds == 86400


# ---------------------------------------------------------------------------
# deploy-cost-reconcile — one Signal per (day, repo)
# ---------------------------------------------------------------------------

RECONCILE_DOC = {
    "schema_version": "1.0.0",
    "record_type": "deploy_cost_reconcile",
    "date": "2026-09-06",
    "generated_at": "2026-09-07T08:40:00Z",
    "tolerance_pct": 15.0,
    "repos": [
        {"repo": "alpha", "deploys": 2, "estimated_minutes": 37,
         "billed_minutes": 35, "delta_minutes": 2, "delta_pct": 5.71,
         "tolerance_pct": 15.0, "reconcile_state": "within_tolerance"},
        {"repo": "beta", "deploys": 1, "estimated_minutes": 2,
         "billed_minutes": 9, "delta_minutes": -7, "delta_pct": -77.78,
         "tolerance_pct": 15.0, "reconcile_state": "over_tolerance"},
        {"repo": "gamma", "deploys": 3, "estimated_minutes": 40,
         "billed_minutes": None, "delta_minutes": None, "delta_pct": None,
         "tolerance_pct": 15.0, "reconcile_state": "unmeasurable"},
    ],
}

_RECONCILE_KEY = "changelog/deploy_cost_reconcile/2026-09-06.json"


def _fetch_reconcile():
    return s3_records.fetch(
        {**_example("deploy-cost-reconcile"), "bucket": BUCKET},
        lister=lambda b, p: [(_RECONCILE_KEY, "2026-09-07T08:41:00+00:00")],
        reader=lambda b, k: RECONCILE_DOC,
        now=NOW)


def test_one_signal_per_repo_per_day_fanned_out_of_the_day_document():
    result = _fetch_reconcile()
    assert result.status is AdapterStatus.OK
    assert set(_by_id(result)) == {
        "2026-09-06:alpha", "2026-09-06:beta", "2026-09-06:gamma"}
    assert all(e.kind is Kind.SIGNAL for e in result.entities)


def test_estimate_bill_and_delta_all_render_on_the_same_row():
    """The estimate is a MODEL of the spend and the billing figure IS the
    spend; a surface showing one without the other cannot be checked."""
    row = _by_id(_fetch_reconcile())["2026-09-06:alpha"].detail["fields"]
    assert row["estimated_minutes"]["value"] == 37
    assert row["billed_minutes"]["value"] == 35
    assert row["delta_minutes"]["value"] == 2
    assert row["delta_pct"]["value"] == 5.71
    assert row["delta_pct"]["baseline"] == 15.0


def test_an_over_tolerance_delta_is_visible_as_its_own_state():
    entities = _by_id(_fetch_reconcile())
    assert entities["2026-09-06:alpha"].state == "within_tolerance"
    assert entities["2026-09-06:beta"].state == "over_tolerance"


def test_an_unsettled_billing_day_renders_unmeasurable_never_zero():
    """`no data` is never rendered as green, and it is never rendered as a
    zero-cost day either (principles §2.7)."""
    gamma = _by_id(_fetch_reconcile())["2026-09-06:gamma"]
    assert gamma.state == "unmeasurable"
    assert gamma.detail["fields"]["billed_minutes"]["value"] is None
    assert gamma.detail["fields"]["delta_pct"]["value"] is None


def test_reconcile_rows_carry_their_repo_facet():
    assert _by_id(_fetch_reconcile())["2026-09-06:beta"].facets["repo"] == "beta"


def test_reconcile_as_of_is_the_documents_generated_at():
    row = _by_id(_fetch_reconcile())["2026-09-06:alpha"]
    assert row.provenance.as_of == "2026-09-07T08:40:00Z"


# ---------------------------------------------------------------------------
# The bindings themselves
# ---------------------------------------------------------------------------


def test_both_bindings_are_s3_records_and_not_a_new_adapter():
    """The boundary test's whole point: four adapters implementing this one
    shape were built in an hour by concurrent sessions and consolidated by
    ruling. A fifth is the same defect."""
    doc = yaml.safe_load(_CONFIG_EXAMPLE.read_text())
    kinds = {b["name"]: b["kind"] for b in doc["adapters"]
             if b["name"].startswith("deploy-cost")}
    assert kinds == {"deploy-cost": "s3-records",
                     "deploy-cost-reconcile": "s3-records"}


def test_key_patterns_match_the_producers_real_key_shapes():
    cost = _example("deploy-cost")["key_pattern"]
    rec = _example("deploy-cost-reconcile")["key_pattern"]
    assert re.search(
        cost, "changelog/deploy_cost/2026-09-06/20260906T120000Z_alpha_aaa111.json")
    assert re.search(rec, "changelog/deploy_cost_reconcile/2026-09-06.json")
    # And do NOT match the entries prefix they sit beside.
    assert not re.search(
        cost, "changelog/entries/2026-09-06/20260906T120000Z_alpha_aaa111.json")
