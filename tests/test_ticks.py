"""Tick lists (`console/index/ticks.py`, `ticks:` + a table's `tick:`).

A table that opts in shows a checkbox per row; a tick POSTs to `/ticks/<list>`
and is saved to the list's declared S3 object, re-read before every write.
Hermetic: the store is an in-memory fake.
"""
from __future__ import annotations

import http.client
import json
import threading

import pytest

from console.index import pages, ticks
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import json as render_json
from console.server.app import _html, serve
from console.server.router import resolve

TICKS = {"reading-list": {"store": "s3://bucket-x/dashboard/reading-list/reads.json",
                          "label": "read"}}
PAGE = {"reading": {"title": "Reading", "question": "What to read?", "range": "all", "sections": [
    {"type": "table", "title": "This week", "fields": ["title"],
     "select": {"facets": {"record": "reading-current"}},
     "tick": {"list": "reading-list", "key": "item_id"}},
]}}


def _item(n):
    return Entity(kind=Kind.RUN, id=f"reading:week:{n}", state=State.HEALTHY,
                  provenance=Provenance("s3://b/index.json", "2026-11-05T17:00:00+00:00"),
                  facets={"record": "reading-current"},
                  detail={"fields": {"title": {"value": f"Paper {n}", "render": "text"},
                                     "item_id": {"value": f"id{n}", "render": "text"}}})


class FakeS3:
    def __init__(self, marks=None):
        self.objects = {}
        if marks is not None:
            self.objects[("bucket-x", "dashboard/reading-list/reads.json")] = json.dumps(
                {"schema_version": 1, "marks": marks}).encode()
        self.puts = []

    def get(self, bucket, key):
        return self.objects.get((bucket, key))

    def put(self, bucket, key, body):
        self.puts.append((bucket, key))
        self.objects[(bucket, key)] = body


def _index(s3):
    decls = ticks.parse(TICKS)
    idx = Index()
    idx.add_result(AdapterResult(name="fixture", status=AdapterStatus.OK,
                                 claim_class=ClaimClass.OBSERVATION,
                                 entities=tuple(_item(n) for n in (1, 2))))
    stores, results = ticks.read(decls, "2026-11-05T17:00:00Z", getter=s3.get)
    for r in results:
        idx.add_result(r)
    ticks.attach(idx, stores)
    pages.attach(idx, pages.parse(PAGE, ticks=set(decls)))
    idx.finalize()
    return idx


@pytest.mark.parametrize("bad, message", [
    ({"Reading": {"store": "s3://b/k.json"}}, "lowercase"),
    ({"r": {"store": "https://b/k.json"}}, "s3://"),
    ({"r": {"store": "s3://b/k.json", "colour": "red"}}, "unknown key"),
])
def test_a_bad_ticks_block_fails_by_name(bad, message):
    with pytest.raises(ticks.TicksConfigError, match=message):
        ticks.parse(bad)


def test_a_table_naming_an_undeclared_list_fails_the_build():
    with pytest.raises(pages.PagesConfigError, match="no `ticks:` list"):
        pages.parse(PAGE, ticks=set())


def test_only_a_table_takes_tick():
    bad = {"p": {"title": "t", "question": "q", "sections": [
        {"type": "chart", "title": "c", "fields": ["x"], "select": {"facets": {"a": "b"}},
         "tick": {"list": "reading-list", "key": "item_id"}}]}}
    with pytest.raises(pages.PagesConfigError, match="only a table takes `tick`"):
        pages.parse(bad, ticks={"reading-list"})


def test_rows_carry_their_mark_in_json_and_a_checkbox_in_html():
    idx = _index(FakeS3({"id1": {"done": True, "at": "x", "by": None}}))
    page = render_json.payload(idx, resolve("/page/reading", ""))["page"]
    sec = page["sections"][0]
    assert sec["tick"]["label"] == "read" and sec["tick"]["writable"]
    marks = {r["id"]: r["tick"] for r in sec["rows"]}
    assert marks["reading:week:1"] == {"key": "id1", "done": True}
    assert marks["reading:week:2"] == {"key": "id2", "done": False}
    html = _html(idx, resolve("/page/reading", ""))
    assert 'action="/ticks/reading-list"' in html
    assert '<th aria-label="read">read</th>' in html
    assert 'name="key" value="id1"' in html and "☑" in html and "☐" in html


def test_an_unreadable_store_is_a_failed_source_and_read_only():
    s3 = FakeS3()
    s3.objects[("bucket-x", "dashboard/reading-list/reads.json")] = b"not json"
    idx = _index(s3)
    sec = render_json.payload(idx, resolve("/page/reading", ""))["page"]["sections"][0]
    assert not sec["tick"]["writable"]
    assert 'action="/ticks/' not in _html(idx, resolve("/page/reading", ""))


def test_tick_rereads_writes_whole_and_unticks():
    s3 = FakeS3({"other": {"done": True}})
    idx = _index(FakeS3())
    allowed = pages.tick_keys(idx, "reading-list")
    assert allowed == {"id1", "id2"}
    ticks.tick(idx, "reading-list", "id2", True, "brian@x", "T1", allowed,
               getter=s3.get, putter=s3.put)
    doc = json.loads(s3.objects[("bucket-x", "dashboard/reading-list/reads.json")])
    assert doc["marks"]["other"] == {"done": True}  # a second writer is not clobbered
    assert doc["marks"]["id2"] == {"done": True, "at": "T1", "by": "brian@x"}
    assert ticks.store(idx, "reading-list").done("id2")
    ticks.tick(idx, "reading-list", "id2", False, None, "T2", allowed,
               getter=s3.get, putter=s3.put)
    assert "id2" not in json.loads(s3.objects[("bucket-x", "dashboard/reading-list/reads.json")])["marks"]


def test_concurrent_ticks_never_erase_each_other():
    """I13239 (codex 23e92b8f13727a91): two ticks racing through the
    read-modify-write each read the same marks, and the later put erased the
    earlier mark. A slow store widens the window so the race is certain."""
    import time

    class SlowS3(FakeS3):
        def get(self, bucket, key):
            body = super().get(bucket, key)
            time.sleep(0.05)
            return body

    s3 = SlowS3()
    idx = _index(FakeS3())
    allowed = pages.tick_keys(idx, "reading-list")
    threads = [threading.Thread(target=ticks.tick, args=(
        idx, "reading-list", k, True, None, "T", allowed), kwargs={
        "getter": s3.get, "putter": s3.put}) for k in ("id1", "id2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    marks = json.loads(s3.objects[("bucket-x", "dashboard/reading-list/reads.json")])["marks"]
    assert set(marks) == {"id1", "id2"}


def test_a_key_no_table_shows_is_refused_and_nothing_is_written():
    s3 = FakeS3()
    idx = _index(s3)
    with pytest.raises(ticks.TickRefused, match="not a row shown"):
        ticks.tick(idx, "reading-list", "id9", True, None, "T", pages.tick_keys(idx, "reading-list"),
                   getter=s3.get, putter=s3.put)
    assert s3.puts == []


# ------------------------------------------------------------------- HTTP ---

@pytest.fixture()
def live(monkeypatch):
    s3 = FakeS3()
    idx = _index(s3)
    real = ticks.tick
    monkeypatch.setattr(ticks, "tick", lambda index, name, key, done, actor, now, allowed, **_:
                        real(index, name, key, done, actor, now, allowed, getter=s3.get, putter=s3.put))
    server = serve(idx, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], s3
    server.shutdown()
    server.server_close()


def _post(port, path, body, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request("POST", path, body=body,
                 headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})
    resp = conn.getresponse()
    out = resp.status, dict(resp.getheaders()), resp.read().decode()
    conn.close()
    return out


def test_a_same_origin_tick_saves_and_returns_to_the_page(live):
    port, s3 = live
    status, headers, _ = _post(port, "/ticks/reading-list", "key=id1&done=1&back=/page/reading",
                               {"Origin": f"http://127.0.0.1:{port}"})
    assert status == 303 and headers["Location"] == "/page/reading"
    assert len(s3.puts) == 1


def test_a_tick_never_redirects_off_the_console(live):
    port, _ = live
    _, headers, _ = _post(port, "/ticks/reading-list", "key=id1&done=1&back=https://evil.test/",
                          {"Origin": f"http://127.0.0.1:{port}"})
    assert headers["Location"] == "/"


def test_a_cross_site_tick_is_refused(live):
    port, s3 = live
    status, _, _ = _post(port, "/ticks/reading-list", "key=id1&done=1",
                         {"Origin": "https://evil.test"})
    assert status == 403 and s3.puts == []


def test_an_undeclared_list_is_404(live):
    port, s3 = live
    status, _, body = _post(port, "/ticks/nope", "key=id1&done=1",
                            {"Origin": f"http://127.0.0.1:{port}"})
    assert status == 404 and "no tick list" in body and s3.puts == []


def test_a_json_tick_answers_with_the_outcome_not_a_redirect(live):
    """The page's script asks for JSON so the reader's page never reloads."""
    port, s3 = live
    status, headers, body = _post(port, "/ticks/reading-list", "key=id1&done=1&back=/page/reading",
                                  {"Origin": f"http://127.0.0.1:{port}",
                                   "Accept": "application/json"})
    assert status == 200 and "Location" not in headers
    assert json.loads(body) == {"ok": True, "key": "id1", "done": True}
    assert len(s3.puts) == 1
    status, _, body = _post(port, "/ticks/reading-list", "key=nope&done=1",
                            {"Origin": f"http://127.0.0.1:{port}", "Accept": "application/json"})
    assert status == 404 and json.loads(body)["ok"] is False and len(s3.puts) == 1
