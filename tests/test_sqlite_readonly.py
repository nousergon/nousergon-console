"""nousergon-console#213 (audit PLAT-09): every SQL path opens its source
read-only and never creates a missing one."""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from console.adapters import sql_query, sql_source
from console.drivers import sql_source as sql_source_driver
from console.model.descriptor import Binding
from console.model.envelope import AdapterStatus
from console.sqlite_readonly import connect_read_only


def _db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (ticker text, n integer)")
    conn.execute("INSERT INTO t VALUES ('AAA', 1)")
    conn.commit()
    conn.close()
    return path


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_missing_path_raises_and_creates_nothing(tmp_path):
    missing = tmp_path / "nope.sqlite"
    with pytest.raises(FileNotFoundError, match="never created"):
        connect_read_only(str(missing))
    assert not missing.exists()


def test_a_connection_cannot_write(tmp_path):
    db = _db(tmp_path / "a.sqlite")
    before = _digest(db)
    conn = connect_read_only(str(db))
    try:
        assert conn.execute("SELECT n FROM t").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES ('BBB', 2)")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE u (x)")
    finally:
        conn.close()
    assert _digest(db) == before


def test_uri_metacharacters_in_a_path_cannot_add_parameters(tmp_path):
    odd = tmp_path / "a?mode=rwc#x %.sqlite"
    _db(odd)
    conn = connect_read_only(str(odd))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM t")
    finally:
        conn.close()
    assert not (tmp_path / "a").exists()


def test_sql_query_adapter_fails_on_a_missing_source_and_creates_nothing(tmp_path):
    missing = tmp_path / "research.db"
    result = sql_query.fetch({
        "db_path": str(missing),
        "queries": [{"name": "q", "entity_kind": "signal",
                     "query": "SELECT ticker FROM t", "id_template": "{ticker}"}],
    })
    assert result.status is AdapterStatus.FAILED
    assert not missing.exists()


def test_sql_source_adapter_fails_on_a_missing_source_and_creates_nothing(tmp_path):
    missing = tmp_path / "research.db"
    result = sql_source.fetch({
        "database": str(missing), "query": "SELECT ticker FROM t",
        "kind": "signal", "id_fields": ["ticker"],
    })
    assert result.status is AdapterStatus.FAILED
    assert not missing.exists()


def test_sql_source_adapter_reads_without_changing_the_file(tmp_path):
    db = _db(tmp_path / "research.db")
    before = _digest(db)
    result = sql_source.fetch({
        "database": str(db), "query": "SELECT ticker, n FROM t",
        "kind": "signal", "id_fields": ["ticker"],
    })
    assert result.status is AdapterStatus.OK and len(result.entities) == 1
    assert _digest(db) == before


def test_sql_source_driver_fails_on_a_missing_source_and_creates_nothing(tmp_path):
    missing = tmp_path / "metrics.sqlite"
    binding = Binding(component_id="writer", kind="metrics", driver="sql-source",
                      spec={"credential": "db", "query": "SELECT n FROM t"})
    result = sql_source_driver.read(binding, {"sql_credentials": {"db": str(missing)}})
    assert not missing.exists()
    assert result.ok is False and "FileNotFoundError" in str(result.error)
