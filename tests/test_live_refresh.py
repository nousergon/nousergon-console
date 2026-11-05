"""Live freshness as progressive enhancement (alpha-engine-config-I12783, plan §3).

`/console.js` ticks every server-stamped age and re-reads the page at the
index's rebuild cadence. These pin the parts that can drift silently: the
script is served from this origin and names no other; every HTML page carries
it with the two metas it reads; and its age wording is the server's
`_age_text`, so a page reads the same before and after the script runs.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from console.index.graph import Index
from console.model.envelope import AdapterResult, AdapterStatus
from console.render import html as render_html
from console.server.app import serve
from tests.fixtures import fixture_graph

SCRIPT = Path(__file__).resolve().parent.parent / "console/static/console.js"


@pytest.fixture()
def live():
    entities, edges = fixture_graph()
    idx = Index()
    idx.add_result(AdapterResult(name="f", status=AdapterStatus.OK,
                                 entities=tuple(entities), edges=tuple(edges)))
    idx.build_info = replace(idx.build_info, built_at="2026-11-03T06:00:00+00:00",
                             refresh_seconds=180.0)
    server = serve(idx, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _get(url: str) -> tuple[str, str]:
    with urllib.request.urlopen(url) as resp:
        return resp.headers["Content-Type"], resp.read().decode("utf-8")


def test_the_script_is_served_from_this_origin_as_javascript(live):
    content_type, body = _get(live + "/console.js")
    assert content_type.startswith("text/javascript")
    assert "function ageText" in body


def test_the_script_names_no_other_origin():
    """No CDN at runtime: a fetch or import of another host is a dependency
    the page would silently lose behind a proxy (and a leak of who reads it)."""
    assert not re.search(r"https?://", SCRIPT.read_text())


def test_every_html_page_carries_the_script_and_its_two_metas(live):
    for path in ("/", "/component", "/component/comp-producer", "/search?q=comp"):
        _, body = _get(live + path)
        head = body[:body.index("</head>")]
        assert re.search(r'<script src="/console\.js\?v=[0-9a-f]{12}" defer></script>', head), path
        assert '<meta name="console-built" content="2026-11-03T06:00:00+00:00">' in head
        assert '<meta name="console-refresh" content="180">' in head


def test_the_metas_match_the_json_representation(live):
    """§3.8: the HTML says nothing the JSON does not."""
    req = urllib.request.Request(live + "/", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req) as resp:
        text = resp.read().decode("utf-8")
    assert "2026-11-03T06:00:00+00:00" in text
    assert re.search(r'"refresh_seconds":\s*180', text)


def test_no_declared_cadence_means_no_refresh_meta():
    out = render_html.head_inject(None)
    assert '<meta name="console-built" content="">' in out
    assert "console-refresh" not in out
    assert out.startswith(render_html.HEAD_INJECT)


def test_the_page_reads_completely_without_the_script(live):
    """Progressive enhancement: the server already printed every age."""
    _, body = _get(live + "/")
    assert '<time datetime="2026-11-03T06:00:00+00:00"' in body
    assert '<span class="age">' in body


_SECONDS = [0, 59, 60, 61, 3599, 3600, 7200, 48 * 3600 - 1, 48 * 3600, 30 * 86400]


def test_the_scripts_age_wording_is_the_servers():
    node = shutil.which("node")
    assert node, "node is required to test console.js (preinstalled on CI runners)"
    now = datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)
    stamps = [(now - timedelta(seconds=s)).isoformat() for s in _SECONDS]
    stamps += [(now + timedelta(hours=5)).isoformat(),
               "2026-11-03T05:51:12.481203+00:00"]
    prog = ("const m=require(process.argv[1]);const now=Date.parse(process.argv[2]);"
            "console.log(JSON.stringify(JSON.parse(process.argv[3]).map(s=>m.ageFrom(s,now))))")
    out = subprocess.run([node, "-e", prog, str(SCRIPT), now.isoformat(), json.dumps(stamps)],
                         capture_output=True, text=True, check=True).stdout
    for stamp, age in zip(stamps, json.loads(out)):
        server = render_html.as_of_html(stamp, now)
        assert f'<span class="age">{age}</span>' in server, (stamp, age, server)


def test_freshness_metas_describe_the_index_the_response_rendered():
    """codex f22b3364478ac5da: `_build_info` re-read `server.index`, so a
    refresh swapping the index mid-render stamped old data with the new
    build's time. It must read the index the request captured."""
    from types import SimpleNamespace

    from console.server.app import ConsoleHandler

    handler = ConsoleHandler.__new__(ConsoleHandler)
    rendered = SimpleNamespace(build_info="old-build")
    handler.server = SimpleNamespace(index=SimpleNamespace(build_info="new-build"))
    handler._served_index = rendered
    assert handler._build_info() == "old-build"
    del handler._served_index
    assert handler._build_info() == "new-build"


def test_open_details_survive_a_refresh_by_identity_not_position():
    """codex e051f22ff1737637: open state was restored by position, and only
    when the count was unchanged, so one new report collapsed every open one
    and a shifted list would open the wrong ones."""
    node = shutil.which("node")
    assert node, "node is required to test console.js (preinstalled on CI runners)"

    def d(text, open_=False, cls="report", id_=""):
        return {"id": id_, "cls": cls, "text": text, "open": open_}

    cases = {
        # A new report arrives at the top: the open one stays open, the new
        # one keeps the server's state.
        "prepended": ([d("Nov 2", True), d("Nov 1")],
                      [d("Nov 3"), d("Nov 2"), d("Nov 1")]),
        # A report drops out: nothing shifts onto its neighbour.
        "dropped": ([d("Nov 3"), d("Nov 2", True), d("Nov 1")],
                    [d("Nov 3"), d("Nov 1")]),
        # The only section of its class keeps its state while a count in its
        # summary moves; a closed one the reader shut stays shut.
        "count moved": ([d("milestones · 3 of 5 met", True, "milestones-summary"),
                         d("cleared rows", False, "")],
                        [d("milestones · 4 of 5 met", False, "milestones-summary"),
                         d("cleared rows", True, "")]),
        # Same summary twice pairs in order; an id wins over text.
        "repeats": ([d("x", True, ""), d("x", False, ""), d("a", True, "", "keep")],
                    [d("x", False, ""), d("x", True, ""), d("renamed", False, "", "keep")]),
    }
    prog = ("const m=require(process.argv[1]);const c=JSON.parse(process.argv[2]);"
            "const o={};for(const k in c)o[k]=m.carryOpen(c[k][0],c[k][1]);"
            "console.log(JSON.stringify(o))")
    out = json.loads(subprocess.run([node, "-e", prog, str(SCRIPT), json.dumps(cases)],
                                    capture_output=True, text=True, check=True).stdout)
    assert out == {
        "prepended": [None, True, False],
        "dropped": [False, False],
        "count moved": [True, False],
        "repeats": [True, False, True],
    }
    # The refresh path uses it, not the old positional copy.
    src = SCRIPT.read_text()
    assert "carryOpen(was, describe(now))" in src
    assert "now.length === open.length" not in src
