"""sql-source ADAPTER tests — many rows, one query, fixture sqlite, hermetic.

Distinct from `tests/test_sql_source_driver.py`, which covers the per-
component `sql-source` DRIVER (one row, one component). This covers
`nousergon-console#58`'s many-row Signal sources (`50_Data_Integrity`,
`Data_and_Maturity`) and the `54_Fleet_SLA` facet (a Signal naming the
Component it measures).
"""
from __future__ import annotations

import sqlite3

from console.adapters import sql_source
from console.config import ADAPTERS
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render.html import entity_page


def _db(tmp_path, rows):
    path = tmp_path / "research.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "create table data_integrity (phase text, ticker text, flagged integer, disagreement_pct real)"
    )
    conn.executemany(
        "insert into data_integrity values (?, ?, ?, ?)", rows,
    )
    conn.commit()
    conn.close()
    return str(path)


def test_many_rows_become_many_signals(tmp_path):
    db = _db(tmp_path, [("l1", "AAPL", 1, 0.4), ("l1", "MSFT", 0, 0.0)])
    result = sql_source.fetch({
        "database": db,
        "query": "SELECT phase, ticker, flagged, disagreement_pct FROM data_integrity",
        "kind": "signal",
        "id_fields": ["phase", "ticker"],
    })
    assert result.status is AdapterStatus.OK
    ids = {e.id for e in result.entities}
    assert ids == {"l1:AAPL", "l1:MSFT"}
    assert all(e.kind is Kind.SIGNAL for e in result.entities)


def test_columns_beyond_the_identifier_become_declared_fields(tmp_path):
    db = _db(tmp_path, [("l1", "AAPL", 1, 0.4)])
    result = sql_source.fetch({
        "database": db,
        "query": "SELECT phase, ticker, flagged, disagreement_pct FROM data_integrity",
        "kind": "signal",
        "id_fields": ["phase", "ticker"],
    })
    fields = result.entities[0].detail["fields"]
    assert fields["disagreement_pct"]["value"] == 0.4
    assert "phase" not in fields  # identifier columns excluded, not duplicated


def test_state_field_carried_verbatim(tmp_path):
    db = _db(tmp_path, [("l1", "AAPL", 1, 0.4)])
    result = sql_source.fetch({
        "database": db,
        "query": "SELECT phase, ticker, flagged FROM data_integrity",
        "kind": "signal",
        "id_fields": ["phase", "ticker"],
        "state_field": "flagged",
    })
    assert result.entities[0].state == "1"


def test_default_state_used_when_no_state_field_declared(tmp_path):
    db = _db(tmp_path, [("l1", "AAPL", 1, 0.4)])
    result = sql_source.fetch({
        "database": db,
        "query": "SELECT phase, ticker FROM data_integrity",
        "kind": "signal",
        "id_fields": ["phase", "ticker"],
    })
    assert result.entities[0].state == "reporting"


def test_field_descriptors_override_the_bare_default(tmp_path):
    db = _db(tmp_path, [("l1", "AAPL", 1, 0.4)])
    result = sql_source.fetch({
        "database": db,
        "query": "SELECT phase, ticker, disagreement_pct FROM data_integrity",
        "kind": "signal",
        "id_fields": ["phase", "ticker"],
        "field_descriptors": {"disagreement_pct": {"unit": "ratio", "baseline": 0.0}},
    })
    field = result.entities[0].detail["fields"]["disagreement_pct"]
    assert field["unit"] == "ratio"
    assert field["baseline"] == 0.0


def test_a_row_missing_part_of_the_identifier_is_skipped_and_named(tmp_path):
    db = _db(tmp_path, [("l1", None, 1, 0.4)])
    result = sql_source.fetch({
        "database": db,
        "query": "SELECT phase, ticker FROM data_integrity",
        "kind": "signal",
        "id_fields": ["phase", "ticker"],
    })
    assert result.entities == ()
    assert "invalid-rows" in result.unavailable


# -------------------------------------------------------------- failures ---


def test_missing_config_is_failed(tmp_path):
    db = _db(tmp_path, [("l1", "AAPL", 1, 0.4)])
    result = sql_source.fetch({"database": db, "query": "SELECT 1"})
    assert result.status is AdapterStatus.FAILED
    assert "kind" in result.unavailable and "id_fields" in result.unavailable


def test_bad_query_is_failed_not_a_crash(tmp_path):
    db = _db(tmp_path, [("l1", "AAPL", 1, 0.4)])
    result = sql_source.fetch({
        "database": db, "query": "SELECT nonexistent_column FROM data_integrity",
        "kind": "signal", "id_fields": ["phase"],
    })
    assert result.status is AdapterStatus.FAILED
    assert "source" in result.unavailable


def test_missing_database_is_failed():
    result = sql_source.fetch({"query": "SELECT 1", "kind": "signal", "id_fields": ["x"]})
    assert result.status is AdapterStatus.FAILED
    assert "database" in result.unavailable


def test_sql_source_adapter_is_registered():
    assert "sql-source" in ADAPTERS


# ---------------------------------------------------- Fleet_SLA facet case --


def test_component_id_field_derives_a_measures_edge_rendered_as_a_facet(tmp_path):
    db = tmp_path / "sla.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("create table sla (process_id text, metric text, hit_rate real)")
    conn.execute("insert into sla values ('preopen', 'first-attempt-30d', 0.95)")
    conn.commit()
    conn.close()

    result = sql_source.fetch({
        "database": str(db),
        "query": "SELECT process_id, metric, hit_rate FROM sla",
        "kind": "signal",
        "id_fields": ["process_id", "metric"],
        "component_id_field": "process_id",
    })
    signal = result.entities[0]
    assert signal.id == "preopen:first-attempt-30d"
    assert (signal.id, "measures", "preopen") in {
        (e.source, e.rel, e.target) for e in result.edges
    }

    component = Entity(kind=Kind.COMPONENT, id="preopen", state=State.HEALTHY,
                        provenance=Provenance(source="registry"))
    idx = Index()
    idx.add_result(AdapterResult(name="registry", status=AdapterStatus.OK,
                                  claim_class=ClaimClass.DECLARATION, entities=(component,)))
    idx.add_result(result)

    # The Signal is reachable FROM the Component via the derived reverse edge
    # (§3.3) — the "facet on the Component page" claim, with no bespoke
    # rendering: it is the same generic `relations` block every entity page
    # already renders.
    related_ids = {e.target for e in idx.related("preopen") if e.rel == "measured-by"}
    assert signal.id in related_ids
    html = entity_page(idx, idx.entity("preopen"))
    assert "measured-by" in html
