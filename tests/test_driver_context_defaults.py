"""Every driver's `context` carries a PRODUCTION reader by default
(alpha-engine-config-I7425, 2026-08-17).

Live finding: `driver:document-fields` FAILED on the box with "no document
reader available" — `document_reader`/`object_stat` reached drivers only via
the `_driver_context` test hook, so three of six drivers could never read
anything in production. The defaults live in one module and are merged UNDER
whatever a test injects.
"""
from __future__ import annotations

import json

import pytest

from console.config import _driver_context
from console.drivers import context as ctx
from console.model.descriptor import Binding
from console.drivers import document_fields, emitted_envelope, s3_records, object_store


def test_the_production_context_carries_both_readers():
    c = _driver_context({})
    assert callable(c["document_reader"])
    assert callable(c["object_stat"])


def test_an_injected_reader_still_wins():
    fake = lambda uri: "{}"  # noqa: E731
    c = _driver_context({"_driver_context": {"document_reader": fake}})
    assert c["document_reader"] is fake


def test_local_files_read_and_stat_without_aws(tmp_path):
    doc = tmp_path / "latest.json"
    doc.write_text(json.dumps({"fields": {"n": {"value": 1}}}))
    read = ctx.default_document_reader()
    stat = ctx.default_object_stat()
    assert json.loads(read(str(doc)))["fields"]["n"]["value"] == 1
    assert json.loads(read(f"file://{doc}"))["fields"]["n"]["value"] == 1
    assert stat(str(doc)) is not None
    assert stat(str(tmp_path / "missing.json")) is None
    with pytest.raises(FileNotFoundError):
        read(str(tmp_path / "missing.json"))


def test_s3_uri_reads_through_the_shared_client_cache(monkeypatch):
    calls = []

    class _Body:
        def read(self):
            return b'{"a": 1}'

    class _Exc(Exception):
        response = {"Error": {"Code": "404"}}

    class _Client:
        class exceptions:
            NoSuchKey = _Exc
            ClientError = _Exc

        def get_object(self, Bucket, Key):
            calls.append(("get", Bucket, Key))
            if Key.endswith("missing.json"):
                raise _Exc()
            return {"Body": _Body()}

        def head_object(self, Bucket, Key):
            calls.append(("head", Bucket, Key))
            if Key.endswith("missing.json"):
                raise _Exc()
            from datetime import datetime, timezone
            return {"LastModified": datetime(2026, 8, 17, tzinfo=timezone.utc)}

    monkeypatch.setattr(ctx, "_boto3_available", lambda: True)
    monkeypatch.setattr(ctx, "_aws_client", lambda service, region=None: _Client())
    read = ctx.default_document_reader()
    stat = ctx.default_object_stat()
    assert json.loads(read("s3://b/reports/latest.json")) == {"a": 1}
    assert stat("s3://b/reports/latest.json") == "2026-08-17T00:00:00+00:00"
    assert stat("s3://b/reports/missing.json") is None
    with pytest.raises(FileNotFoundError):
        read("s3://b/reports/missing.json")
    assert calls[0] == ("get", "b", "reports/latest.json")


def _binding(driver, spec):
    return Binding(component_id="comp-x", driver=driver, kind="component", spec=spec)


@pytest.mark.parametrize("driver,spec", [
    (document_fields, {"documents": [{"key": "PATH"}]}),
    (s3_records, {"key": "PATH", "kind": "signal", "records_path": "rows",
                  "id_template": "{name}"}),
    (emitted_envelope, {"key": "PATH"}),
    (object_store, {"key": "PATH"}),
])
def test_every_reading_driver_reads_a_local_document_through_the_default_context(tmp_path, driver, spec):
    doc = tmp_path / "doc.json"
    doc.write_text(json.dumps({"schema_version": 1, "component_id": "comp-x",
                               "state": "HEALTHY", "as_of": "2026-08-17T00:00:00Z",
                               "fields": {"n": {"value": 1}},
                               "rows": [{"name": "r1", "value": 2}]}))
    spec = json.loads(json.dumps(spec).replace("PATH", str(doc)))
    result = driver.read(_binding(driver.name, spec), _driver_context({}))
    assert "no document reader" not in (result.error or ""), result.error
    assert "no object-store reader" not in (result.error or ""), result.error
