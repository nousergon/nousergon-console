"""Research Content & Scheduling slice (nousergon-console#61) — proves each of
the 7 migrated views' declared question is answerable by an entity of the
right kind/id, reachable through the generic per-kind list + entity panes
(§4.1's three-tier nav: Overview -> Domain(kind) -> Entity), carrying the
row contract (§5.1) and self-describing fields (§5.8).

Two mechanisms, both generic and already shipped — no new adapter code:

- `s3-records` (`console/adapters/s3_records.py`, landed via #70/#73/#75/#77):
  14_RAG_Inventory.py, 43_Distillation_Corpus.py, 44_Think_Tank.py,
  45_Morning_Signal_Schedule.py, Director_Plan.py.
- `object-store` (already shipped; this PR adds only a `question` config
  key, mirroring `s3-records`' own synthetic-declared-field convention):
  17_Research_Briefing_Archive.py, Daily_News.py — both non-JSON bodies
  (markdown, parquet) that `s3-records` cannot parse.

Field/path names below are read from the real crucible-dashboard producer/
loader pair for each view (see `config.example.yaml`'s own per-view comments
for the exact source citations), not invented.
"""
from __future__ import annotations

from datetime import datetime, timezone

from console.adapters import object_store, s3_records
from console.model.envelope import AdapterStatus
from console.model.kinds import Kind

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
BUCKET = "fixture-bucket"


def _by_id(result):
    return {e.id: e for e in result.entities}


# ------------------------------------------------- 1. 14_RAG_Inventory.py --
# "How large and fresh is the RAG corpus backing the research agents, by
# source and ticker?" — Artifact, `rag/manifest/{date}.json`.

_RAG_MANIFEST = {
    "generated_at": "2026-08-09T05:00:00Z",
    "totals": {"documents": 1200, "chunks": 9000, "tickers": 300},
    "by_ticker_coverage": {"tickers_with_any_doc": 300, "p50_docs_per_ticker": 4},
    "embedding": {"model": "voyage-3-lite", "dimension": 512},
}


def _rag_cfg():
    return {
        "bucket": BUCKET,
        "prefix": "rag/manifest/",
        "kind": "artifact",
        "question": "How large and fresh is the RAG corpus backing the research agents, by source and ticker?",
        "key_pattern": r"rag/manifest/(?P<date>\d{4}-\d{2}-\d{2})\.json$",
        "id_template": "rag-manifest:{date}",
        "as_of_field": "generated_at",
        "cadence": "7d",
        "fields": {
            "documents": {"path": "totals.documents", "render": "count"},
            "tickers_with_any_doc": {"path": "by_ticker_coverage.tickers_with_any_doc", "render": "count"},
            "embedding_model": {"path": "embedding.model", "render": "text"},
        },
    }


def test_rag_inventory_one_artifact_per_manifest_with_nested_aggregates():
    def lister(b, p):
        return [("rag/manifest/2026-08-09.json", "2026-08-09T05:01:00Z")]

    def reader(b, k):
        return _RAG_MANIFEST

    result = s3_records.fetch(_rag_cfg(), lister=lister, reader=reader, now=NOW)
    assert result.status is AdapterStatus.OK
    ent = result.entities[0]
    assert ent.kind is Kind.ARTIFACT
    # Namespaced (§3.6) — a bare `{date}` would collide with the Cycle
    # entities the schedule views below mint for the same calendar date.
    assert ent.id == "rag-manifest:2026-08-09"
    fields = ent.detail["fields"]
    assert fields["documents"]["value"] == 1200
    assert fields["tickers_with_any_doc"]["value"] == 300
    assert fields["embedding_model"]["value"] == "voyage-3-lite"
    assert fields["question"]["value"].startswith("How large and fresh")


def test_rag_inventory_never_reads_per_doc_or_chunk_content():
    """The manifest's own disclosure boundary (per-doc lists, chunk text stay
    in pgvector) is respected by construction: only the configured top-level/
    nested paths above are ever read, never arbitrary body content."""
    def lister(b, p):
        return [("rag/manifest/2026-08-09.json", "2026-08-09T05:01:00Z")]

    def reader(b, k):
        return {**_RAG_MANIFEST, "per_ticker_documents": {"AAPL": ["10-K 2026"]}}

    result = s3_records.fetch(_rag_cfg(), lister=lister, reader=reader, now=NOW)
    assert "per_ticker_documents" not in result.entities[0].detail["fields"]


# --------------------------------------- 2. 17_Research_Briefing_Archive.py --
# "What did the research morning-briefing email say on a given date, vs.
# recent weeks?" — Artifact, `consolidated/{date}/morning.md` (markdown —
# `object-store`, freshness-only, question via the new synthetic field).

def test_research_briefing_archive_freshness_only_artifact_with_question():
    cfg = {
        "bucket": BUCKET,
        "prefix": "consolidated/",
        "question": "What did the research morning-briefing email say on a given date, vs. recent weeks?",
        "key_pattern": r"consolidated/(?P<date>[^/]+)/morning\.md$",
        "cadence": "7d",
    }

    def lister(b, p):
        return [("consolidated/2026-08-09/morning.md", "2026-08-09T13:00:00Z")]

    result = object_store.fetch(cfg, lister=lister, now=NOW)
    assert result.status is AdapterStatus.OK
    ent = result.entities[0]
    assert ent.kind is Kind.ARTIFACT
    assert ent.id == "consolidated/2026-08-09/morning.md"
    assert ent.detail["fields"]["question"]["value"].startswith("What did the research morning-briefing")


# ------------------------------------------- 3. 43_Distillation_Corpus.py --
# "How close is the SFT distillation corpus to the ~1000-pair kill-gate
# trigger?" — Signal, `decision_artifacts/distillation/corpus_stats/latest.json`.

def _corpus_cfg():
    return {
        "bucket": BUCKET,
        "prefix": "decision_artifacts/distillation/corpus_stats/",
        "kind": "signal",
        "question": "How close is the SFT distillation corpus to the ~1000-pair kill-gate trigger?",
        "key_pattern": r"decision_artifacts/distillation/corpus_stats/latest\.json$",
        # `generated_at` is top-level; `capture.last_captured_date` is nested
        # and NOT reachable by id_template (only `fields`/`as_of_field`
        # resolve dotted paths).
        "id_template": "{generated_at}",
        "as_of_field": "generated_at",
        "cadence": "7d",
        "fields": {
            "deduped_single_teacher_pairs": {
                "path": "trigger.deduped_single_teacher", "unit": "pairs",
                "baseline": 1000, "render": "count",
            },
            "crossed": {"path": "trigger.crossed", "render": "text"},
        },
    }


def test_distillation_corpus_id_from_nested_body_field_not_the_key():
    def lister(b, p):
        return [("decision_artifacts/distillation/corpus_stats/latest.json", "2026-08-09T12:00:00Z")]

    def reader(b, k):
        return {"generated_at": "2026-08-09T12:00:00Z",
                "trigger": {"deduped_single_teacher": 812, "target_pairs": 1000, "crossed": False}}

    result = s3_records.fetch(_corpus_cfg(), lister=lister, reader=reader, now=NOW)
    assert result.status is AdapterStatus.OK
    ent = result.entities[0]
    assert ent.kind is Kind.SIGNAL
    assert ent.id == "2026-08-09T12:00:00Z"  # from the body, not "latest.json"
    fields = ent.detail["fields"]
    assert fields["deduped_single_teacher_pairs"] == {
        "value": 812, "unit": "pairs", "baseline": 1000, "render": "count",
    }


# --------------------------------------------------------- 4. 44_Think_Tank.py --
# "How does the daily think tank's independent rating compare to the
# scanner's attractiveness score?" — Signal, (ticker, trading_day).

def _thinktank_cfg():
    return {
        "bucket": BUCKET,
        "prefix": "thinktank/ratings/",
        "kind": "signal",
        "question": "How does the daily think tank's independent rating compare to the scanner's attractiveness score?",
        "key_pattern": r"thinktank/ratings/(?P<trading_day>\d{4}-\d{2}-\d{2})\.json$",
        # `rows` is a ticker-keyed OBJECT (`load_thinktank_ratings()` ->
        # `{"rows": {"AAPL": {...}}}`), not an array — the grouped `*`
        # mechanism covers a dict-of-records.
        "records_path": "rows.*",
        "group_field": "ticker",
        "id_template": "{trading_day}:{ticker}",
        "cadence": "1d",
        "fields": {
            "rating": {"path": "rating", "unit": "score", "render": "value"},
            "attractiveness_score": {"path": "attractiveness_score", "unit": "score", "render": "value"},
            "delta_vs_scanner": {"path": "rating_minus_attractiveness", "render": "value"},
        },
    }


def test_think_tank_dict_of_tickers_expands_to_one_signal_per_ticker():
    def lister(b, p):
        return [("thinktank/ratings/2026-08-09.json", "2026-08-09T20:00:00Z")]

    def reader(b, k):
        return {"rows": {
            "AAPL": {"rating": 7.2, "attractiveness_score": 6.9, "rating_minus_attractiveness": 0.3},
            "MSFT": {"rating": 5.1, "attractiveness_score": 5.8, "rating_minus_attractiveness": -0.7},
        }}

    result = s3_records.fetch(_thinktank_cfg(), lister=lister, reader=reader, now=NOW)
    assert result.status is AdapterStatus.OK
    by_id = _by_id(result)
    assert set(by_id) == {"2026-08-09:AAPL", "2026-08-09:MSFT"}
    assert all(e.kind is Kind.SIGNAL for e in result.entities)
    aapl = by_id["2026-08-09:AAPL"].detail["fields"]
    assert aapl["rating"]["value"] == 7.2
    assert aapl["delta_vs_scanner"]["value"] == 0.3


# ---------------------------------------------- 5. 45_Morning_Signal_Schedule.py --
# "What is the morning-signal podcast scheduled to air on a given day, and
# did it air?" — Cycle, calendar date. Read-only by design (§5.6): the write
# path (the calendar editor) is out of scope; two claim-merging instances
# answer "what's scheduled" and "did it air" from independent sources.

def _schedule_cfg():
    return {
        "bucket": BUCKET,
        "prefix": "schedule/",
        "kind": "cycle",
        "question": "What is the morning-signal podcast scheduled to air on a given day?",
        "key_pattern": r"schedule/schedule\.json$",
        "records_path": "entries.*",
        "group_field": "date",
        "id_template": "{date}",
        "state_field": "mode",
        "fields": {"topic": {"path": "topic", "render": "text"}},
    }


def _applied_cfg():
    return {
        "bucket": BUCKET,
        "prefix": "schedule/applied/",
        "kind": "cycle",
        "key_pattern": r"schedule/applied/(?P<date>\d{4}-\d{2}-\d{2})(?:-(?:am|pm))?\.json$",
        "id_template": "{date}",
        "state_default": "aired",
    }


def test_schedule_manifest_expands_dated_entries_to_cycles():
    def lister(b, p):
        return [("schedule/schedule.json", "2026-08-09T00:00:00Z")]

    def reader(b, k):
        return {"schema_version": 1, "entries": {
            "2026-08-09": {"mode": "override", "topic": "CPI print"},
        }}

    result = s3_records.fetch(_schedule_cfg(), lister=lister, reader=reader, now=NOW)
    assert result.status is AdapterStatus.OK
    ent = result.entities[0]
    assert ent.kind is Kind.CYCLE
    assert ent.id == "2026-08-09"
    assert ent.state == "override"
    assert ent.detail["fields"]["topic"]["value"] == "CPI print"


def test_a_date_with_no_schedule_entry_has_no_cycle_here():
    """Regular programming (the default — no entry) has no entity from this
    instance. That absence is the fact (§5.5), not a gap: only dates with an
    override/extend/skip decision are worth indexing."""
    def lister(b, p):
        return [("schedule/schedule.json", "2026-08-09T00:00:00Z")]

    def reader(b, k):
        return {"schema_version": 1, "entries": {}}

    result = s3_records.fetch(_schedule_cfg(), lister=lister, reader=reader, now=NOW)
    assert result.entities == ()


def test_applied_marker_normalizes_edition_suffix_to_the_bare_date():
    def lister(b, p):
        return [("schedule/applied/2026-08-09-am.json", "2026-08-09T09:00:00Z")]

    def reader(b, k):
        return {}

    result = s3_records.fetch(_applied_cfg(), lister=lister, reader=reader, now=NOW)
    ent = result.entities[0]
    assert ent.id == "2026-08-09"
    assert ent.state == "aired"


# ----------------------------------------------------------- 6. Daily_News.py --
# "What news stories, sentiment, and event flags fired today for the
# held+tracked universe?" — Artifact, (run_id/date). Parquet — object-store,
# freshness-only.

def test_daily_news_freshness_only_artifact_with_question():
    cfg = {
        "bucket": BUCKET,
        "prefix": "data/news_articles_daily/",
        "question": "What news stories, sentiment, and event flags fired today for the held+tracked universe?",
        "key_pattern": r"data/news_articles_daily/(?P<run_id>[^/]+)_articles\.parquet$",
        "cadence": "1d",
    }

    def lister(b, p):
        return [("data/news_articles_daily/run123_articles.parquet", "2026-08-09T14:00:00Z")]

    result = object_store.fetch(cfg, lister=lister, now=NOW)
    ent = result.entities[0]
    assert ent.kind is Kind.ARTIFACT
    assert ent.detail["run_id"] == "run123"
    assert ent.detail["fields"]["question"]["value"].startswith("What news stories")


# ----------------------------------------------------------- 7. Director_Plan.py --
# "What did the weekly Director advisory pass propose, and what carried over
# from last week?" — Decision, run date -> action_plan.json items.

def _director_cfg():
    return {
        "bucket": BUCKET,
        "prefix": "director/",
        "kind": "decision",
        "question": "What did the weekly Director advisory pass propose, and what carried over from last week?",
        "key_pattern": r"director/(?P<run_date>[^/]+)/action_plan\.json$",
        "records_path": "action_items",
        # action_items carry no stable id (unlike the companion
        # carryover_ledger.json, whose items do) — title is best-effort.
        "id_template": "{run_date}:{title}",
        "state_field": "status",
        "fields": {
            "priority": {"path": "priority", "render": "text"},
            "confidence": {"path": "confidence", "unit": "score", "render": "value"},
        },
    }


def test_director_plan_expands_action_items_to_decisions():
    def lister(b, p):
        return [("director/2026-08-09/action_plan.json", "2026-08-09T18:00:00Z")]

    def reader(b, k):
        return {"run_date": "2026-08-09", "action_items": [
            {"title": "trim AAPL", "priority": "P1", "status": "proposed", "confidence": 72},
            {"title": "hold MSFT", "priority": "P2", "status": "carried_over", "confidence": 55},
        ]}

    result = s3_records.fetch(_director_cfg(), lister=lister, reader=reader, now=NOW)
    assert result.status is AdapterStatus.OK
    by_id = _by_id(result)
    assert set(by_id) == {"2026-08-09:trim AAPL", "2026-08-09:hold MSFT"}
    assert all(e.kind is Kind.DECISION for e in result.entities)
    assert by_id["2026-08-09:hold MSFT"].state == "carried_over"
    assert by_id["2026-08-09:trim AAPL"].detail["fields"]["priority"]["value"] == "P1"


# ----------------------------------------------------- cross-slice safety --

def test_rag_manifest_and_schedule_cycle_do_not_collide_on_a_bare_date():
    """A regression guard for the collision this slice's own config found
    during manual build_index verification (§3.6): rag-inventory's id is
    namespaced precisely because an unnamespaced `{date}` would collide with
    the Cycle entities `morning-signal-schedule`/`-applied` mint for the
    same calendar date once both are enabled."""
    rag_result = s3_records.fetch(
        _rag_cfg(),
        lister=lambda b, p: [("rag/manifest/2026-08-09.json", "2026-08-09T05:01:00Z")],
        reader=lambda b, k: _RAG_MANIFEST,
        now=NOW,
    )
    schedule_result = s3_records.fetch(
        _schedule_cfg(),
        lister=lambda b, p: [("schedule/schedule.json", "2026-08-09T00:00:00Z")],
        reader=lambda b, k: {"entries": {"2026-08-09": {"mode": "override"}}},
        now=NOW,
    )
    rag_ids = {e.id for e in rag_result.entities}
    schedule_ids = {e.id for e in schedule_result.entities}
    assert rag_ids.isdisjoint(schedule_ids)
