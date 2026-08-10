"""Migration proof for `nousergon-console#55` — Research & Signals slice (10
`crucible-dashboard` views, mapping table `docs/dashboard-migration-mapping.md`
§4 S2). Exercises the actual query/record bindings shipped in
`config.example.yaml`'s `research-db` / `universe-board` /
`attractiveness-trends` / `universe-membership` entries against small fixture
data, asserting one representative entity per view — the same adapters, the
same query text, the same identifier scheme a real deployment would run.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from console.adapters import object_store, object_store_records, sql_query
from console.model.kinds import Kind, State

_CONFIG_EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


def _bindings():
    """The four config entries this slice added, pulled live from
    `config.example.yaml` — a query-text change there is a query-text change
    the test then exercises, never a fixture drifting from the shipped config."""
    with open(_CONFIG_EXAMPLE) as fh:
        cfg = yaml.safe_load(fh)
    by_name = {a["name"]: a for a in cfg["adapters"]}
    return by_name["research-db"], by_name["universe-board"], by_name["attractiveness-trends"], by_name["universe-membership"]


RESEARCH_DB, UNIVERSE_BOARD, ATTRACTIVENESS_TRENDS, UNIVERSE_MEMBERSHIP = _bindings()


def _sql_runner(rows_by_query_name):
    """Routes each configured query to its fixture rows by matching the
    query's own declared `name` against the table it selects FROM — avoids
    hardcoding query text a second time in the test."""
    query_text_by_name = {q["name"]: q["query"] for q in RESEARCH_DB["config"]["queries"]}

    def runner(db_path, query_text):
        for qname, text in query_text_by_name.items():
            if text.strip() == query_text.strip():
                return rows_by_query_name.get(qname, [])
        raise AssertionError(f"unrecognized query: {query_text!r}")
    return runner


# 1. 2_Signals_and_Research.py — Signal (ticker, signal_date)
def test_signals_and_research_score_history():
    result = sql_query.fetch(
        RESEARCH_DB["config"],
        runner=_sql_runner({
            "score-history": [{"ticker": "ACME", "score_date": "2026-08-01", "score": 0.7}],
        }),
    )
    entity = next(e for e in result.entities
                  if e.provenance.source == "sql-query:/path/to/research.db:score-history")
    assert entity.kind is Kind.SIGNAL
    assert entity.id == "ACME:2026-08-01"


# 2. 11_Signal_Lifecycle.py — Signal (ticker, signal_date), merges with #1's identifier
def test_signal_lifecycle_thesis_merges_with_score_history_identifier():
    result = sql_query.fetch(
        RESEARCH_DB["config"],
        runner=_sql_runner({
            "score-history": [{"ticker": "ACME", "score_date": "2026-08-01", "score": 0.7}],
            "investment-thesis": [{"ticker": "ACME", "score_date": "2026-08-01",
                                    "thesis_summary": "bull case", "conviction": "high",
                                    "price_target": 200.0}],
        }),
    )
    matching = [e for e in result.entities if e.id == "ACME:2026-08-01"]
    assert len(matching) == 2  # two claims about the same identifier, per §2.5
    assert all(e.kind is Kind.SIGNAL for e in matching)


# 3+4. 29_Decision_Review.py + 31_CIO_Review.py — the SAME Decision entity
# (ticker, eval_date): one query binding satisfies both dashboard questions.
def test_decision_review_and_cio_review_share_one_decision_entity():
    result = sql_query.fetch(
        RESEARCH_DB["config"],
        runner=_sql_runner({
            "cio-decisions": [{
                "ticker": "ACME", "eval_date": "2026-08-01", "team_id": "technology",
                "cio_decision": "ADVANCE", "cio_rank": 1, "cio_conviction": "high",
                "final_score": 0.9, "rationale": "strong momentum",
                "sector": "technology", "quant_filter_pass": 1, "filter_fail_reason": None,
            }],
        }),
    )
    entity = next(e for e in result.entities
                  if e.provenance.source == "sql-query:/path/to/research.db:cio-decisions")
    assert entity.kind is Kind.DECISION
    assert entity.id == "ACME:2026-08-01"
    assert entity.state == "ADVANCE"
    assert entity.facets == {"team_id": "technology"}


# 5. 33_Sector_Team_Review.py — Run (eval_date, team_id)
def test_sector_team_review_run_with_candidate_drilldown():
    candidates = json.dumps([{"ticker": "ACME", "quant_rank": 1, "quant_score": 0.8,
                               "qual_score": 0.7, "team_recommended": 1}])
    result = sql_query.fetch(
        RESEARCH_DB["config"],
        runner=_sql_runner({
            "sector-team-runs": [{"eval_date": "2026-08-01", "team_id": "technology",
                                   "n_candidates": 1, "n_recommended": 1,
                                   "candidates_json": candidates}],
        }),
    )
    entity = next(e for e in result.entities
                  if e.provenance.source == "sql-query:/path/to/research.db:sector-team-runs")
    assert entity.kind is Kind.RUN
    assert entity.id == "technology:2026-08-01"
    assert entity.state is State.HEALTHY
    assert entity.detail["candidates_json"] == [
        {"ticker": "ACME", "quant_rank": 1, "quant_score": 0.8,
         "qual_score": 0.7, "team_recommended": 1},
    ]


# 6. 34_Scanner.py — Run (eval_date)
def test_scanner_cycle_run_with_ticker_drilldown():
    tickers = json.dumps([{"ticker": "ACME", "sector": "technology", "tech_score": 88,
                            "quant_filter_pass": 1, "filter_fail_reason": None}])
    result = sql_query.fetch(
        RESEARCH_DB["config"],
        runner=_sql_runner({
            "scanner-cycles": [{"eval_date": "2026-08-01", "n_total": 1, "n_quant_pass": 1,
                                 "n_liquidity_pass": 1, "n_volatility_pass": 1,
                                 "n_balance_sheet_pass": 1, "tickers_json": tickers}],
        }),
    )
    entity = next(e for e in result.entities
                  if e.provenance.source == "sql-query:/path/to/research.db:scanner-cycles")
    assert entity.kind is Kind.RUN
    assert entity.id == "2026-08-01"
    assert entity.state is State.HEALTHY
    assert entity.detail["tickers_json"][0]["ticker"] == "ACME"


# 7. 39_Universe_Board.py — Artifact (ticker)
def test_universe_board_one_artifact_per_ticker():
    body = {
        "as_of": "2026-08-09T12:00:00Z",
        "stocks": [
            {"ticker": "ACME", "sector": "technology", "gate": {"quant_filter_pass": True}},
        ],
    }
    result = object_store_records.fetch(
        UNIVERSE_BOARD["config"],
        reader=lambda bucket, key: body,
    )
    (entity,) = result.entities
    assert entity.kind is Kind.ARTIFACT
    assert entity.id == "ACME"
    assert entity.state == "True"
    assert entity.facets == {"sector": "technology"}


# 8. 40_Attractiveness_Trends.py — Signal (ticker, as_of)
def test_attractiveness_trends_keyed_by_ticker_and_as_of():
    body = {
        "as_of": "2026-08-09T00:00:00Z",
        "stocks": [{"ticker": "ACME", "sector": "technology", "rising": True, "attr_slope": 0.4}],
    }
    result = object_store_records.fetch(
        ATTRACTIVENESS_TRENDS["config"],
        reader=lambda bucket, key: body,
    )
    (entity,) = result.entities
    assert entity.kind is Kind.SIGNAL
    assert entity.id == "ACME:2026-08-09T00:00:00Z"


# 9. 5_Focus_List.py — Signal (eval_date, focus_team_id); honest empty-state
# before the source schema migration is zero entities, not a bespoke placeholder.
def test_focus_list_weekly_summary():
    result = sql_query.fetch(
        RESEARCH_DB["config"],
        runner=_sql_runner({
            "focus-list-weekly": [{"eval_date": "2026-08-01", "focus_team_id": "technology",
                                    "n_focus_list": 5, "n_overrides": 1}],
        }),
    )
    entity = next(e for e in result.entities
                  if e.provenance.source == "sql-query:/path/to/research.db:focus-list-weekly")
    assert entity.kind is Kind.SIGNAL
    assert entity.id == "2026-08-01:technology"


def test_focus_list_pre_migration_is_honestly_empty_not_a_placeholder():
    result = sql_query.fetch(
        RESEARCH_DB["config"],
        runner=_sql_runner({"focus-list-weekly": []}),
    )
    matching = [e for e in result.entities
               if e.provenance.source == "sql-query:/path/to/research.db:focus-list-weekly"]
    assert matching == []


# 10. 55_Universe_Churn.py — Artifact (`universe_membership/{date}/membership.json`)
# reuses the EXISTING object-store adapter, unmodified — no new code for this view.
def test_universe_churn_reuses_existing_object_store_adapter():
    def lister(bucket, prefix):
        return [("universe_membership/2026-08-09/membership.json", "2026-08-09T00:00:00Z")]

    result = object_store.fetch(UNIVERSE_MEMBERSHIP["config"], lister=lister)
    (entity,) = result.entities
    assert entity.kind is Kind.ARTIFACT
    assert entity.id == "universe_membership/2026-08-09/membership.json"
    assert entity.detail["run_date"] == "2026-08-09"
