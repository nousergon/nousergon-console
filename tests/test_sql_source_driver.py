"""The SQL driver reads declared metrics without accepting executable descriptors."""
from __future__ import annotations

import sqlite3

import pytest

from console.config import build_index
from console.model.descriptor import DescriptorError, parse
from console.render.html import entity_page
from console.render.json import entity as json_entity


def _config(tmp_path, query, **binding):
    registry = tmp_path / "registry.d"
    registry.mkdir(parents=True)
    (registry / "writer.yaml").write_text(
        "component_id: writer\nmetrics:\n  driver: sql-source\n"
        + "\n".join(f"  {key}: {value!r}" for key, value in {"credential": "metrics-db", "query": query, **binding}.items())
        + "\n"
    )
    db = tmp_path / "metrics.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("create table observations (rows_written integer)")
    conn.execute("insert into observations values (903)")
    conn.commit()
    conn.close()
    return {"registry": {"adapter": "yaml-directory", "path": str(registry)}, "sql_credentials": {"metrics-db": str(db)}}


def test_sql_source_renders_a_declared_query_column_as_a_metric(tmp_path):
    index = build_index(_config(tmp_path, "SELECT rows_written FROM observations"))

    assert index.entity("writer").detail["fields"]["rows_written"] == 903


@pytest.mark.parametrize("query", ["DELETE FROM observations", "SELECT 1; SELECT 2"])
def test_sql_source_rejects_non_single_select_descriptors(query):
    with pytest.raises(DescriptorError, match="sql-source query"):
        parse({"metrics": {"driver": "sql-source", "credential": "db", "query": query}}, "writer")


def test_sql_source_rejects_inline_credentials():
    with pytest.raises(DescriptorError, match="inline credential"):
        parse({"metrics": {"driver": "sql-source", "connection_string": "postgres://user:password@host/db", "query": "SELECT 1"}}, "writer")


def test_sql_source_distinguishes_empty_result_from_missing_column(tmp_path):
    empty = build_index(_config(tmp_path / "empty", "SELECT rows_written FROM observations WHERE 1 = 0", field="rows_written")).entity("writer")
    missing = build_index(_config(tmp_path / "missing", "SELECT 1 AS other", field="rows_written")).entity("writer")

    assert empty.detail["sql_source"]["condition"] == "empty-result"
    assert missing.detail["sql_source"]["condition"] == "missing-column"
    assert "empty-result" in entity_page(build_index(_config(tmp_path / "html", "SELECT rows_written FROM observations WHERE 1 = 0", field="rows_written")), empty)
    assert json_entity(missing)["detail"]["sql_source"]["condition"] == "missing-column"
