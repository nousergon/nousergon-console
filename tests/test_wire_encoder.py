"""The wire encoder is one chokepoint (config-I7432, 2026-08-17).

A `declared-registry` row carrying `created_at: 2026-08-10` reached the index
as a `datetime.date` (YAML parses it that way), `detail` passed it through
verbatim as §5.8 requires, and `json.dumps` raised inside the request handler
— the live console dropped every JSON connection and the deploy prove read
the surface as an empty index. The fix is not "coerce dates in that adapter":
every adapter reads a source that can hold a date, so the boundary that spells
non-JSON values is the serializer, once, for every emission site.
"""
from __future__ import annotations

import ast
import json
import pathlib
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind
from console.render import json as render_json
from console.server.app import serve
from console.server.router import resolve

ROOT = pathlib.Path(__file__).resolve().parents[1] / "console"


def _index_with(detail: dict) -> Index:
    idx = Index()
    idx.add_result(AdapterResult(
        name="registry", status=AdapterStatus.OK,
        claim_class=ClaimClass.DECLARATION,
        entities=(Entity(kind=Kind.ARTIFACT, id="ops/x/{date}.json",
                         state="declared",
                         provenance=Provenance("file:///r.yaml", None, "file:///r.yaml"),
                         detail=detail),),
    ))
    return idx


def test_a_date_and_a_datetime_in_detail_serialize_as_iso_8601():
    idx = _index_with({"created_at": date(2026, 8, 10),
                       "last_seen": datetime(2026, 8, 17, 0, 47, tzinfo=timezone.utc),
                       "size": Decimal("12.5"), "tags": {"b", "a"}})
    body = render_json.dumps(render_json.payload(idx, resolve("/artifact/ops/x/{date}.json", "")))
    doc = json.loads(body)
    detail = doc["entity"]["detail"] if "entity" in doc else doc["detail"]
    assert detail["created_at"] == "2026-08-10"
    assert detail["last_seen"] == "2026-08-17T00:47:00+00:00"
    assert detail["size"] == 12.5
    assert detail["tags"] == ["a", "b"]


def test_the_cli_dump_uses_the_same_encoder():
    from console.__main__ import _dump
    idx = _index_with({"created_at": date(2026, 8, 10)})
    assert '"created_at": "2026-08-10"' in _dump(idx)


def test_a_value_the_wire_cannot_spell_still_raises_naming_the_type():
    class Opaque:  # no unambiguous textual form
        pass
    with pytest.raises(TypeError, match="Opaque"):
        render_json.dumps({"x": Opaque()})


def test_every_json_emission_site_goes_through_the_encoder():
    """No console module calls `json.dumps`/`json.dump` on its own — a second
    serializer is a second spelling of a date, and the two disagree exactly
    when a new type shows up."""
    offenders = []
    for path in ROOT.rglob("*.py"):
        if path == ROOT / "render" / "json.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"dumps", "dump"}
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in {"json", "jsonlib"}):
                offenders.append(f"{path.relative_to(ROOT.parent)}:{node.lineno}")
    assert offenders == [], offenders


@pytest.fixture()
def live(monkeypatch):
    idx = _index_with({"created_at": date(2026, 8, 10)})
    server = serve(idx, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield idx, f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_over_http_a_dated_registry_row_answers_200_json(live):
    _, base = live
    status, doc = _get_json(f"{base}/")
    assert status == 200
    status, doc = _get_json(f"{base}/artifact/ops/x/{{date}}.json")
    assert status == 200


def test_over_http_an_unspellable_value_is_a_500_in_json_not_a_dropped_connection(live, monkeypatch):
    idx, base = live

    def boom(index, req):
        return {"schema_version": 1, "bad": object()}
    monkeypatch.setattr(render_json, "payload", boom)
    req = urllib.request.Request(f"{base}/", headers={"Accept": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req)
    assert ei.value.code == 500
    doc = json.loads(ei.value.read().decode("utf-8"))
    assert doc["error"] == "render_failed"
    assert "object" in doc["detail"]
