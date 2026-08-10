"""sql-query adapter tests — recorded row fixtures, no live database."""
from __future__ import annotations

from console.adapters import sql_query
from console.model.envelope import AdapterStatus
from console.model.kinds import Kind, State


def _runner(rows_by_query):
    def runner(db_path, query):
        assert db_path == "fixture.db"
        for needle, rows in rows_by_query.items():
            if needle in query:
                return list(rows)
        raise AssertionError(f"unexpected query: {query!r}")
    return runner


def _cfg(queries):
    return {"db_path": "fixture.db", "queries": queries}


# ---------------------------------------------------------------- signals --


def test_signal_rows_carry_raw_state_and_as_of():
    rows = [{"ticker": "ACME", "score_date": "2026-08-01", "score": 0.7}]
    result = sql_query.fetch(
        _cfg([{
            "name": "score-history", "entity_kind": "signal",
            "query": "SELECT ticker, score_date, score FROM score_performance",
            "id_template": "{ticker}:{score_date}", "as_of_field": "score_date",
        }]),
        runner=_runner({"score_performance": rows}),
    )
    assert result.status is AdapterStatus.OK
    (entity,) = result.entities
    assert entity.kind is Kind.SIGNAL
    assert entity.id == "ACME:2026-08-01"
    assert entity.state == "reporting"  # no state_field configured
    assert entity.provenance.as_of == "2026-08-01"
    assert entity.detail["score"] == 0.7


def test_signal_state_field_is_raw_verbatim():
    rows = [{"ticker": "ACME", "eval_date": "2026-08-01", "cio_decision": "ADVANCE"}]
    result = sql_query.fetch(
        _cfg([{
            "name": "cio-decisions", "entity_kind": "decision",
            "query": "SELECT ticker, eval_date, cio_decision FROM cio_evaluations",
            "id_template": "{ticker}:{eval_date}", "state_field": "cio_decision",
        }]),
        runner=_runner({"cio_evaluations": rows}),
    )
    (entity,) = result.entities
    assert entity.kind is Kind.DECISION
    assert entity.state == "ADVANCE"


def test_two_queries_same_identifier_merge_as_separate_claims():
    """Two claims about one id are a normal §2.5 merge, not a collision —
    the index (not this adapter) resolves them; the adapter's job is just to
    emit both claims correctly."""
    result = sql_query.fetch(
        _cfg([
            {
                "name": "score-history", "entity_kind": "signal",
                "query": "SELECT ticker, score_date, score FROM score_performance",
                "id_template": "{ticker}:{score_date}",
            },
            {
                "name": "investment-thesis", "entity_kind": "signal",
                "query": "SELECT ticker, score_date, thesis_summary FROM investment_thesis",
                "id_template": "{ticker}:{score_date}",
            },
        ]),
        runner=_runner({
            "score_performance": [{"ticker": "ACME", "score_date": "2026-08-01", "score": 0.7}],
            "investment_thesis": [{"ticker": "ACME", "score_date": "2026-08-01", "thesis_summary": "x"}],
        }),
    )
    ids = [e.id for e in result.entities]
    assert ids == ["ACME:2026-08-01", "ACME:2026-08-01"]
    sources = {e.provenance.source for e in result.entities}
    assert sources == {"sql-query:fixture.db:score-history", "sql-query:fixture.db:investment-thesis"}


# -------------------------------------------------------------------- run --


def test_run_default_state_when_row_exists():
    rows = [{"team_id": "technology", "eval_date": "2026-08-01", "n_candidates": 12}]
    result = sql_query.fetch(
        _cfg([{
            "name": "sector-team-runs", "entity_kind": "run",
            "query": "SELECT team_id, eval_date, n_candidates FROM team_candidates GROUP BY team_id",
            "id_template": "{team_id}:{eval_date}", "default_state": "HEALTHY",
        }]),
        runner=_runner({"team_candidates": rows}),
    )
    (entity,) = result.entities
    assert entity.kind is Kind.RUN
    assert entity.state is State.HEALTHY


def test_run_state_map_translates_raw_column():
    rows = [{"eval_date": "2026-08-01", "outcome": "ok"}]
    result = sql_query.fetch(
        _cfg([{
            "name": "cycles", "entity_kind": "run",
            "query": "SELECT eval_date, outcome FROM cycle_log",
            "id_template": "{eval_date}", "state_field": "outcome",
            "state_map": {"ok": "HEALTHY", "error": "FAILED"},
        }]),
        runner=_runner({"cycle_log": rows}),
    )
    (entity,) = result.entities
    assert entity.state is State.HEALTHY


def test_run_with_no_placement_renders_unreported_and_declares_unavailable():
    rows = [{"eval_date": "2026-08-01"}]
    result = sql_query.fetch(
        _cfg([{
            "name": "cycles", "entity_kind": "run",
            "query": "SELECT eval_date FROM cycle_log",
            "id_template": "{eval_date}",
            # no state_field, no state_map, no default_state
        }]),
        runner=_runner({"cycle_log": rows}),
    )
    (entity,) = result.entities
    assert entity.state is State.UNREPORTED
    assert "cycles:state" in result.unavailable


def test_run_state_map_miss_renders_unreported():
    rows = [{"eval_date": "2026-08-01", "outcome": "weird"}]
    result = sql_query.fetch(
        _cfg([{
            "name": "cycles", "entity_kind": "run",
            "query": "SELECT eval_date, outcome FROM cycle_log",
            "id_template": "{eval_date}", "state_field": "outcome",
            "state_map": {"ok": "HEALTHY"},
        }]),
        runner=_runner({"cycle_log": rows}),
    )
    (entity,) = result.entities
    assert entity.state is State.UNREPORTED


# ------------------------------------------------------------- json_columns --


def test_json_columns_decoded_into_detail():
    import json
    rows = [{
        "eval_date": "2026-08-01",
        "tickers_json": json.dumps([{"ticker": "ACME", "quant_filter_pass": 1}]),
    }]
    result = sql_query.fetch(
        _cfg([{
            "name": "scanner-cycles", "entity_kind": "run",
            "query": "SELECT eval_date, tickers_json FROM scanner_evaluations GROUP BY eval_date",
            "id_template": "{eval_date}", "default_state": "HEALTHY",
            "json_columns": ["tickers_json"],
        }]),
        runner=_runner({"scanner_evaluations": rows}),
    )
    (entity,) = result.entities
    assert entity.detail["tickers_json"] == [{"ticker": "ACME", "quant_filter_pass": 1}]


# ------------------------------------------------------------------ facets --


def test_facets_and_detail_columns():
    rows = [{"ticker": "ACME", "eval_date": "2026-08-01", "team_id": "technology", "cio_rank": 1}]
    result = sql_query.fetch(
        _cfg([{
            "name": "cio-decisions", "entity_kind": "decision",
            "query": "SELECT ticker, eval_date, team_id, cio_rank FROM cio_evaluations",
            "id_template": "{ticker}:{eval_date}",
            "facets": {"team_id": "team_id"},
            "detail_columns": ["cio_rank"],
        }]),
        runner=_runner({"cio_evaluations": rows}),
    )
    (entity,) = result.entities
    assert entity.facets == {"team_id": "technology"}
    assert entity.detail == {"cio_rank": 1}


# ------------------------------------------------------------------- fail --


def test_missing_db_path_fails():
    result = sql_query.fetch({"queries": [{"name": "x", "entity_kind": "signal",
                                            "query": "SELECT 1", "id_template": "{x}"}]})
    assert result.status is AdapterStatus.FAILED
    assert result.unavailable == ("db_path",)


def test_missing_queries_fails():
    result = sql_query.fetch({"db_path": "fixture.db"})
    assert result.status is AdapterStatus.FAILED
    assert result.unavailable == ("queries",)


def test_non_select_query_rejected():
    result = sql_query.fetch(_cfg([{
        "name": "bad", "entity_kind": "signal",
        "query": "DELETE FROM score_performance", "id_template": "{x}",
    }]))
    assert result.status is AdapterStatus.FAILED
    assert "bad" in result.unavailable[0]


def test_multi_statement_query_rejected():
    result = sql_query.fetch(_cfg([{
        "name": "bad", "entity_kind": "signal",
        "query": "SELECT 1; DROP TABLE score_performance", "id_template": "{x}",
    }]))
    assert result.status is AdapterStatus.FAILED


def test_runner_exception_marks_query_unavailable_but_other_queries_still_run():
    def runner(db_path, query):
        if "broken" in query:
            raise RuntimeError("boom")
        return [{"ticker": "ACME", "score_date": "2026-08-01"}]

    result = sql_query.fetch(_cfg([
        {"name": "broken-query", "entity_kind": "signal",
         "query": "SELECT * FROM broken_table", "id_template": "{ticker}"},
        {"name": "good-query", "entity_kind": "signal",
         "query": "SELECT ticker, score_date FROM score_performance",
         "id_template": "{ticker}:{score_date}"},
    ]), runner=runner)
    assert result.status is AdapterStatus.OK
    assert "broken-query" in result.unavailable
    assert len(result.entities) == 1


def test_all_queries_fail_marks_adapter_failed():
    def runner(db_path, query):
        raise RuntimeError("boom")

    result = sql_query.fetch(_cfg([{
        "name": "x", "entity_kind": "signal", "query": "SELECT 1", "id_template": "{x}",
    }]), runner=runner)
    assert result.status is AdapterStatus.FAILED


# --------------------------------------------------------- default runner --


def test_default_runner_reads_a_real_sqlite_file(tmp_path):
    import sqlite3
    db_path = tmp_path / "research.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE score_performance (symbol text, score_date text, score real)")
    conn.execute("INSERT INTO score_performance VALUES ('ACME', '2026-08-01', 0.7)")
    conn.commit()
    conn.close()

    result = sql_query.fetch({
        "db_path": str(db_path),
        "queries": [{
            "name": "score-history", "entity_kind": "signal",
            "query": "SELECT symbol AS ticker, score_date, score FROM score_performance",
            "id_template": "{ticker}:{score_date}",
        }],
    })
    assert result.status is AdapterStatus.OK
    (entity,) = result.entities
    assert entity.id == "ACME:2026-08-01"
    assert entity.detail["score"] == 0.7
