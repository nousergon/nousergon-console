"""The listed-body cache — a body is re-downloaded only when its LIST entry
changed, and a key gone from the LIST is never served from memory
(alpha-engine-config-I11792).

Driven through each adapter's real boto3-backed lister/reader against a fake
S3 client, so what is proven is the production read path, not just the class.
CI installs only the ``test`` extra, so boto3/botocore are faked here.
"""
from __future__ import annotations

import io
import json
import sys
import types
from datetime import datetime, timezone

import pytest

from console import s3_body_cache
from console.adapters import checks_envelope, s3_records

BUCKET = "fixture-bucket"
PREFIX = "ops/checks/"
LM = datetime(2026, 7, 31, 11, 0, tzinfo=timezone.utc)


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.gets: list[str] = []

    def put(self, key: str, body: dict, etag: str) -> None:
        self.objects[key] = (json.dumps(body).encode(), etag)

    def list_objects_v2(self, **kwargs):
        prefix = kwargs.get("Prefix", "")
        return {"Contents": [
            {"Key": k, "LastModified": LM, "ETag": etag, "Size": len(raw)}
            for k, (raw, etag) in sorted(self.objects.items()) if k.startswith(prefix)
        ]}

    def get_object(self, Bucket, Key):  # noqa: N803 — boto3's spelling
        self.gets.append(Key)
        raw, etag = self.objects[Key]
        return {"Body": io.BytesIO(raw), "ETag": etag, "LastModified": LM,
                "ContentLength": len(raw)}


@pytest.fixture
def s3(monkeypatch):
    exc = types.ModuleType("botocore.exceptions")
    exc.BotoCoreError = type("BotoCoreError", (Exception,), {})
    exc.ClientError = type("ClientError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "boto3", types.ModuleType("boto3"))
    monkeypatch.setitem(sys.modules, "botocore", types.ModuleType("botocore"))
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exc)
    fake = FakeS3()
    for mod in (checks_envelope, s3_records):
        monkeypatch.setattr(mod, "_aws_client", lambda _service: fake)
    s3_body_cache.BODY_CACHE.clear()
    yield fake
    s3_body_cache.BODY_CACHE.clear()


def _envelope(cid: str, summary: str) -> dict:
    return {"schema_version": 1, "check_id": cid, "status": "ok",
            "ran_at": "2026-07-31T11:30:00+00:00", "summary": summary}


def _checks_pass() -> dict[str, str]:
    """One adapter pass; returns {component id: summary}."""
    lister, reader = checks_envelope._default_s3()
    res = checks_envelope.fetch({"bucket": BUCKET, "prefix": PREFIX},
                                lister=lister, reader=reader)
    return {e.id: e.detail["summary"] for e in res.entities if e.kind.value == "component"}


def test_unchanged_etag_issues_no_get(s3):
    s3.put(f"{PREFIX}a/latest.json", _envelope("a", "first"), '"e1"')
    s3.put(f"{PREFIX}b/latest.json", _envelope("b", "first"), '"e2"')
    assert _checks_pass() == {"a": "first", "b": "first"}
    assert len(s3.gets) == 2

    assert _checks_pass() == {"a": "first", "b": "first"}
    assert len(s3.gets) == 2  # second pass: LIST only, zero GETs


def test_changed_etag_is_fetched_again(s3):
    key = f"{PREFIX}a/latest.json"
    s3.put(key, _envelope("a", "first"), '"e1"')
    s3.put(f"{PREFIX}b/latest.json", _envelope("b", "first"), '"e2"')
    _checks_pass()
    s3.gets.clear()

    s3.put(key, _envelope("a", "second"), '"e1-new"')
    assert _checks_pass() == {"a": "second", "b": "first"}
    assert s3.gets == [key]  # only the changed key


def test_removed_key_is_dropped_and_never_served(s3):
    gone = f"{PREFIX}a/latest.json"
    s3.put(gone, _envelope("a", "first"), '"e1"')
    s3.put(f"{PREFIX}b/latest.json", _envelope("b", "first"), '"e2"')
    _checks_pass()

    del s3.objects[gone]
    assert _checks_pass() == {"b": "first"}
    cache = s3_body_cache.BODY_CACHE
    assert (BUCKET, gone) not in cache._bodies
    assert (BUCKET, gone) not in cache._listed

    # Recreated with the SAME etag: the dropped bytes are not resurrected.
    s3.put(gone, _envelope("a", "recreated"), '"e1"')
    s3.gets.clear()
    assert _checks_pass()["a"] == "recreated"
    assert s3.gets == [gone]


def test_s3_records_reader_shares_the_rule(s3):
    key = "reports/2026-07-31/eod.json"
    s3.put(key, {"day": "2026-07-31", "v": 1}, '"r1"')
    lister, reader, _ = s3_records._default_s3()
    cfg = {"bucket": BUCKET, "prefix": "reports/", "kind": "cycle",
           "key_pattern": r"reports/(?P<day>[^/]+)/eod\.json$"}
    for _ in range(3):
        res = s3_records.fetch(cfg, lister=lister, reader=reader)
        assert [e.id for e in res.entities] == ["2026-07-31"]
    assert s3.gets == [key]

    s3.put(key, {"day": "2026-07-31", "v": 2}, '"r2"')
    s3_records.fetch(cfg, lister=lister, reader=reader)
    assert s3.gets == [key, key]


def test_a_key_never_listed_is_always_fetched():
    cache = s3_body_cache.ListedBodyCache()
    calls = []

    def fetch():
        calls.append(1)
        return b"x", "etag:1"

    assert cache.read("b", "k", fetch) == b"x"
    assert cache.read("b", "k", fetch) == b"x"
    assert len(calls) == 2


def test_body_rewritten_between_list_and_get_is_filed_under_its_new_tag():
    cache = s3_body_cache.ListedBodyCache()
    cache.observe_listing("b", "p/", {"p/k": "etag:old"})
    cache.read("b", "p/k", lambda: (b"new", "etag:new"))
    # Same stale listing again: the new bytes must not pass as the old version.
    calls = []
    cache.read("b", "p/k", lambda: (calls.append(1) or b"new", "etag:new"))
    assert calls == [1]
    # The next listing shows the new tag: now it is a hit.
    cache.observe_listing("b", "p/", {"p/k": "etag:new"})
    assert cache.read("b", "p/k", lambda: pytest.fail("should not GET")) == b"new"


def test_listing_tag_falls_back_to_last_modified_and_size():
    assert s3_body_cache.listing_tag({"ETag": '"x"'}) == 'etag:"x"'
    assert s3_body_cache.listing_tag({"LastModified": LM, "Size": 3}) == f"lm:{LM.isoformat()}|3"
    assert s3_body_cache.listing_tag({"LastModified": LM}) is None


def test_s3_records_text_reader_shares_the_cache(s3):
    key = "reports/plan-2026-07-31.md"
    s3.objects[key] = ("# plan\n".encode(), '"t1"')
    lister, _, text_reader = s3_records._default_s3()
    cfg = {"bucket": BUCKET, "prefix": "reports/", "kind": "run", "format": "object",
           "body": "text", "state_default": "HEALTHY", "id_template": "plan@{day}",
           "key_pattern": r"reports/plan-(?P<day>[^/]+)\.md$"}
    for _ in range(3):
        res = s3_records.fetch(cfg, lister=lister, reader=lambda b, k: None,
                               text_reader=text_reader)
        assert res.entities[0].detail["document"]["text"] == "# plan\n"
    assert s3.gets == [key]
