"""Predictor & Model Lifecycle slice (nousergon-console#56) — proves each of
the 7 migrated views' declared question is answerable by an entity of the
right kind/id, reachable through the generic per-kind list + entity panes
(§4.1's three-tier nav: Overview -> Domain(kind) -> Entity), carrying the
row contract (§5.1) and self-describing fields (§5.8).

Two mechanisms, both generic and configured (no fleet topology, §1.1):

- `document-fields` (driver): 7_Predictor.py -> one Component, two
  deliberately split source documents (nousergon-console#56's own gotcha).
- `dated-snapshot` (adapter): 15_Regime.py / 35_Model_Zoo.py /
  36_Predictor_Training.py / 46_Experiments.py -> one Signal or Run per
  dated snapshot.

`13_Feature_Store.py` and `Predictor_Archives.py` (both Artifact-kind, list-
shaped identifiers) need no new code at all — they are `object-store`
(already shipped, already tested in `test_object_store.py`) pointed at the
right prefix/pattern via config, asserted here with the real key shapes from
the issue table to prove the fit, not the mechanism.
"""
from __future__ import annotations

from datetime import datetime, timezone

from console.adapters import dated_snapshot, object_store
from console.config import build_index
from console.drivers import document_fields
from console.model.descriptor import Binding
from console.model.kinds import Kind, State

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------- 1. 7_Predictor.py ----

def test_predictor_component_combines_the_two_split_sources(tmp_path):
    """Declared question: 'Is the live predictor model healthy, and did the
    most recent training run get promoted?' `manifest.json` (training) and
    `latest.json` (rolling/operational) are deliberately split at the source
    (issue's own gotcha) and must both land on one `component_id=predictor`."""
    registry = tmp_path / "registry.d"
    registry.mkdir()
    (registry / "predictor.yaml").write_text(
        "component_id: predictor\n"
        "lifecycle: in-service\n"
        "metrics:\n"
        "  driver: document-fields\n"
        "  documents:\n"
        "    - key: s3://research/predictor/metrics/latest.json\n"
        "      fields:\n"
        "        model_version: {path: model_version, render: text}\n"
        "        hit_rate_30d_rolling: {unit: ratio, baseline: 0.55, render: ratio}\n"
        "    - key: s3://research/predictor/weights/meta/manifest.json\n"
        "      fields:\n"
        "        promoted: {render: text}\n"
        "        last_trained: {render: text}\n"
        "  cadence_minutes: 1440\n"
    )
    docs = {
        "s3://research/predictor/metrics/latest.json": {
            "model_version": "meta-v9", "hit_rate_30d_rolling": 0.61,
        },
        "s3://research/predictor/weights/meta/manifest.json": {
            "promoted": True, "last_trained": "2026-08-08",
        },
    }
    config = {
        "registry": {"adapter": "yaml-directory", "path": str(registry)},
        "_driver_context": {"document_reader": docs.get, "now": NOW},
    }
    index = build_index(config)
    ent = index.entity("predictor")
    assert ent.kind is Kind.COMPONENT
    assert ent.conflicts == ()  # two documents combine, no false disagreement
    fields = ent.detail["fields"]
    assert fields["model_version"]["value"] == "meta-v9"
    assert fields["promoted"]["value"] is True
    assert fields["last_trained"]["value"] == "2026-08-08"
    # Reachable via the generic per-kind list (structure path, §3.1).
    assert "predictor" in {e.id for e in index.of_kind(Kind.COMPONENT)}


# ------------------------------------------------ 2. 13_Feature_Store.py ---

def test_feature_store_snapshots_are_artifacts_via_object_store():
    """Declared question: 'Which pre-computed inference features are
    fresh/covered...?' Identifier: `features/{date}/*.parquet`,
    `features/registry.json` — a list-shaped Artifact question, answered by
    the existing generic `object-store` adapter with no new code."""
    def lister(bucket, prefix):
        return [
            ("features/2026-08-08/technical.parquet", "2026-08-08T05:00:00Z"),
            ("features/2026-08-08/macro.parquet", "2026-08-08T05:00:00Z"),
            ("features/registry.json", "2026-07-01T00:00:00Z"),
        ]
    cfg = {
        "bucket": "research", "prefix": "features/",
        "key_pattern": r"features/(?P<date>[^/]+)/(?P<file>[^/]+\.parquet)",
        "cadence": "1d",
    }
    result = object_store.fetch(cfg, lister=lister, now=NOW)
    ids = {e.id for e in result.entities}
    assert "features/2026-08-08/technical.parquet" in ids
    assert all(e.kind is Kind.ARTIFACT for e in result.entities)
    # registry.json intentionally excluded by this key_pattern — a second
    # object-store entry (its own literal key_pattern) covers it, exactly the
    # "one adapter, many config entries" reuse §2.3 expects.
    reg_cfg = {**cfg, "key_pattern": r"features/registry\.json"}
    reg_result = object_store.fetch(reg_cfg, lister=lister, now=NOW)
    assert {e.id for e in reg_result.entities} == {"features/registry.json"}


# ------------------------------------------------------- 3. 15_Regime.py ---

def test_regime_signal_carries_weekly_state_verbatim():
    """Declared question: 'What regime state does the HMM substrate show
    this week, does it agree with the macro agent, and is a change signal
    firing?' Identifier: weekly run_id / calendar_date — a Signal, not a
    Component (regime state is a domain value, not a §8.3 member)."""
    def lister(bucket, prefix):
        return [("regime_substrate/2026-08-08.json", "2026-08-08T00:00:00Z")]
    def reader(key):
        return {
            "regime": "risk-on", "agrees_with_macro": True,
            "change_signal_firing": False,
        }
    cfg = {
        "bucket": "research", "prefix": "regime_substrate/",
        "key_pattern": r"regime_substrate/(?P<date>[^/]+)\.json",
        "id_template": "regime:{date}",
        "kind": "signal",
        "state_field": "regime",
        "fields": {
            "agrees_with_macro": {"render": "text"},
            "change_signal_firing": {"render": "text"},
        },
        "cadence": "7d",
    }
    result = dated_snapshot.fetch(cfg, lister=lister, document_reader=reader, now=NOW)
    ent = result.entities[0]
    assert ent.kind is Kind.SIGNAL
    assert ent.id == "regime:2026-08-08"
    assert ent.state == "risk-on"  # verbatim, not forced into §8.3 (§5.1)
    assert ent.detail["fields"]["agrees_with_macro"]["value"] is True


# ---------------------------------------------------- 4. 35_Model_Zoo.py ---

def test_model_zoo_leaderboard_becomes_one_run_per_cycle():
    """Declared question: 'Which model won this cycle's champion/challenger
    contest, and how has rotation trended?' Identifier: (leaderboard date,
    version_id) — a Run, resolving to §8.3."""
    def lister(bucket, prefix):
        return [("predictor/model_zoo/leaderboard/2026-08-08.json", "2026-08-08T06:00:00Z")]
    def reader(key):
        return {"champion_version": "v9", "promoted": True, "rotation_delta": "none"}
    cfg = {
        "bucket": "research", "prefix": "predictor/model_zoo/leaderboard/",
        "key_pattern": r"predictor/model_zoo/leaderboard/(?P<date>[^/]+)\.json",
        "id_template": "model-zoo:{date}",
        "kind": "run",
        "state_field": "promoted",
        "state_map": {"True": "HEALTHY", "False": "DEGRADED"},
        "fields": {"champion_version": {"render": "text"}},
        "cadence": "1d",
    }
    result = dated_snapshot.fetch(cfg, lister=lister, document_reader=reader, now=NOW)
    ent = result.entities[0]
    assert ent.kind is Kind.RUN
    assert ent.state is State.HEALTHY
    assert ent.detail["fields"]["champion_version"]["value"] == "v9"


# --------------------------------------------- 5. 36_Predictor_Training.py -

def test_predictor_training_run_reflects_the_ic_gate_verdict():
    """Declared question: 'Did this cycle's base-retrain pass the IC gate and
    get promoted?' Identifier: (run_date, model_version) — a Run."""
    def lister(bucket, prefix):
        return [("predictor/metrics/training_summary_2026-08-08.json", "2026-08-08T06:00:00Z")]
    def reader(key):
        return {"ic_gate_passed": False, "promoted": False, "model_version": "v10"}
    cfg = {
        "bucket": "research", "prefix": "predictor/metrics/",
        "key_pattern": r"predictor/metrics/training_summary_(?P<date>[^/]+)\.json",
        "id_template": "predictor-training:{date}",
        "kind": "run",
        "state_field": "promoted",
        "state_map": {"True": "HEALTHY", "False": "DEGRADED"},
        "fields": {"ic_gate_passed": {"render": "text"}, "model_version": {"render": "text"}},
        "cadence": "7d",
    }
    result = dated_snapshot.fetch(cfg, lister=lister, document_reader=reader, now=NOW)
    ent = result.entities[0]
    assert ent.kind is Kind.RUN
    assert ent.state is State.DEGRADED  # not promoted, named honestly
    assert ent.detail["fields"]["ic_gate_passed"]["value"] is False


# ------------------------------------------------- 6. 46_Experiments.py ----

def test_experiments_producer_leaderboard_signal_names_the_live_arm():
    """Declared question: 'How do champion-loop challengers perform against
    the live champion, and which arm is live?' Identifier: (spec/arm,
    cohort_date) — a Signal."""
    def lister(bucket, prefix):
        return [("producer_leaderboard/2026-08-08.json", "2026-08-08T00:00:00Z")]
    def reader(key):
        return {"live_arm": "champion", "challenger_score": 0.31, "champion_score": 0.34}
    cfg = {
        "bucket": "research", "prefix": "producer_leaderboard/",
        "key_pattern": r"producer_leaderboard/(?P<date>[^/]+)\.json",
        "id_template": "experiments:{date}",
        "kind": "signal",
        "state_field": "live_arm",
        "fields": {
            "challenger_score": {"unit": "value", "render": "value"},
            "champion_score": {"unit": "value", "render": "value"},
        },
        "cadence": "1d",
    }
    result = dated_snapshot.fetch(cfg, lister=lister, document_reader=reader, now=NOW)
    ent = result.entities[0]
    assert ent.kind is Kind.SIGNAL
    assert ent.state == "champion"
    assert ent.detail["fields"]["challenger_score"]["value"] == 0.31


# --------------------------------------------- 7. Predictor_Archives.py ----

def test_predictor_archives_span_two_prefixes_as_artifacts():
    """Declared question: 'What historical predictor prediction runs and
    weekly training summaries have been produced?' Identifier: date —
    Artifact, list-shaped, two source prefixes -> two object-store entries."""
    def predictions_lister(bucket, prefix):
        return [("predictor/predictions/2026-08-07.json", "2026-08-07T21:00:00Z")]
    def training_lister(bucket, prefix):
        return [("predictor/metrics/training_summary_2026-08-08.json", "2026-08-08T06:00:00Z")]
    predictions_cfg = {
        "bucket": "research", "prefix": "predictor/predictions/",
        "key_pattern": r"predictor/predictions/(?P<date>[^/]+)\.json", "cadence": "1d",
    }
    training_cfg = {
        "bucket": "research", "prefix": "predictor/metrics/",
        "key_pattern": r"predictor/metrics/training_summary_(?P<date>[^/]+)\.json", "cadence": "7d",
    }
    predictions = object_store.fetch(predictions_cfg, lister=predictions_lister, now=NOW)
    training = object_store.fetch(training_cfg, lister=training_lister, now=NOW)
    ids = {e.id for e in (*predictions.entities, *training.entities)}
    assert ids == {
        "predictor/predictions/2026-08-07.json",
        "predictor/metrics/training_summary_2026-08-08.json",
    }
    assert all(e.kind is Kind.ARTIFACT for e in (*predictions.entities, *training.entities))
