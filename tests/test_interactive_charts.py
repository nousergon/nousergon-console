"""Interactive charts over the server's SVG (alpha-engine-config-I12783, plan §3).

`/charts.js` layers a vendored uPlot canvas over every page chart and the
exception trend: hover with exact values, a series toggle held in the URL
fragment, drag to zoom. The SVG is still rendered in full, so a page reads
completely with script off. These tests pin the vendored library by digest,
keep it same-origin, check the SVG carries exactly the points it draws, and
run the script's pure helpers under node.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from console.index.graph import Index
from console.model.envelope import AdapterResult, AdapterStatus
from console.model.fields import Field
from console.render import html as render_html
from console.server.app import _STATIC, serve
from tests.fixtures import fixture_graph

STATIC = Path(__file__).resolve().parent.parent / "console/static"
SCRIPT = STATIC / "charts.js"
CSS = (STATIC / "styles.css").read_text()

#: uPlot 1.6.32 from the npm tarball (registry integrity sha512-KIMVnG68zvu5…),
#: byte for byte. A change here is a library upgrade and is reviewed as one.
VENDORED = {
    "vendor/uplot/uPlot.iife.min.js": "19c8d4c6ad88929a79f4ae49d6f7161566dfd0ba3d15cc495e974f787eb78f1f",
    "vendor/uplot/uPlot.min.css": "df630c6a8d6f8eeaff264b50f73ce5b114f646ffd9a0bb74f049b0a00135fa04",
    "vendor/uplot/LICENSE": "8f989229699b4fe2f1a0432d0e9edc338a8a911e250e2d1b01ecd770a5f5b1bd",
}


def _chart(markup: str) -> dict:
    m = re.search(r'data-chart="([^"]*)"', markup)
    assert m, markup
    return json.loads(html.unescape(m.group(1)))


@pytest.fixture()
def live():
    entities, edges = fixture_graph()
    idx = Index()
    idx.add_result(AdapterResult(name="f", status=AdapterStatus.OK,
                                 entities=tuple(entities), edges=tuple(edges)))
    server = serve(idx, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("name, digest", sorted(VENDORED.items()))
def test_the_library_is_vendored_pinned_and_licensed(name, digest):
    assert hashlib.sha256((STATIC / name).read_bytes()).hexdigest() == digest
    assert "/" + name in _STATIC
    assert "MIT License" in (STATIC / "vendor/uplot/LICENSE").read_text()


def test_the_library_and_the_layer_are_served_from_this_origin(live):
    for path, kind in (("/charts.js", "text/javascript"),
                       ("/vendor/uplot/uPlot.iife.min.js", "text/javascript"),
                       ("/vendor/uplot/uPlot.min.css", "text/css"),
                       ("/vendor/uplot/LICENSE", "text/plain")):
        with urllib.request.urlopen(live + path) as resp:
            assert resp.headers["Content-Type"].startswith(kind), path
            assert resp.read(), path


def test_the_layer_names_no_other_origin():
    """No CDN at runtime: the library loads from /vendor/ or not at all."""
    assert not re.search(r"https?://", SCRIPT.read_text())
    assert '"/vendor/uplot/uPlot.iife.min.js"' in SCRIPT.read_text()


def test_every_html_page_defers_the_layer(live):
    with urllib.request.urlopen(live + "/") as resp:
        body = resp.read().decode("utf-8")
    head = body[:body.index("</head>")]
    assert '<script src="/charts.js" defer></script>' in head


def test_a_timeseries_svg_carries_the_points_it_draws_at_full_precision():
    f = Field(name="nav", value=[["2026-10-01", 1.23456789], ["2026-10-02", None],
                                 ["2026-10-03", -0.5]],
              unit="USD", render="timeseries", of="target")
    whole = Field(name="target", value=2, unit="USD", render="value")
    out = render_html._timeseries_html(f, whole)
    assert out.count("<rect") == 2  # the gap is not drawn, and is not zero
    data = _chart(out)
    assert data == {
        "key": "nav", "unit": "USD",
        "labels": ["2026-10-01", "2026-10-02", "2026-10-03"],
        "series": [
            {"label": "nav", "kind": "bar", "values": [1.23456789, None, -0.5]},
            {"label": "target", "kind": "line", "values": [2.0, 2.0, 2.0]},
        ],
    }
    # The SVG's own text stays the no-script reading.
    assert "<title>2026-10-01: 1.235 USD</title>" in out
    assert 'class="ts-summary"' in out


def test_a_non_finite_point_is_a_gap_on_the_wire():
    f = Field(name="x", value=[1.0, float("nan")], render="timeseries")
    assert _chart(render_html._timeseries_html(f, None))["series"][0]["values"] == [1.0, None]


def test_the_exception_trend_has_a_published_and_a_live_series():
    points = [SimpleNamespace(date="2026-10-30", total=7),
              SimpleNamespace(date="2026-10-31", total=5)]
    data = _chart(render_html.exception_trend(points, now_total=4))
    assert data["labels"] == ["2026-10-30", "2026-10-31", "now"]
    assert data["series"] == [
        {"label": "published daily", "kind": "bar", "sparse": True, "values": [7, 5, None]},
        {"label": "now (live)", "kind": "bar", "sparse": True, "values": [None, None, 4]},
    ]
    assert [s["label"] for s in _chart(render_html.exception_trend(points))["series"]] \
        == ["published daily"]


def test_the_layer_colours_are_tokens_in_both_schemes():
    dark = CSS[CSS.index("@media (prefers-color-scheme: dark)"):]
    for name in ("chart-1", "chart-2", "chart-3", "chart-ref"):
        assert f"--{name}:" in CSS and f"--{name}:" in dark, name
        assert f'"{name}"' in SCRIPT.read_text()
    assert "svg.ts-chart.ts-enhanced { display: none; }" in CSS


def test_the_planner_draws_from_tokens_not_literals():
    src = (Path(render_html.__file__)).read_text()
    assert not re.findall(r"#[0-9a-fA-F]{6}\b", src)
    used = set(re.findall(r"--plan-[\w-]+", src))
    assert used
    for token in used:
        assert f"{token}:" in CSS, token


def test_the_scripts_pure_helpers():
    node = shutil.which("node")
    assert node, "node is required to test charts.js (preinstalled on CI runners)"
    prog = r"""
const m = require(process.argv[1]);
const hidden = {"s3": {"s3_budget": true}, "a~b": {"x,y": true}};
const hash = m.hashFor(hidden, "#keep&off=old~gone");
console.log(JSON.stringify({
  exact: m.formatValue(1.23456789, "USD"),
  gap: m.formatValue(null, "USD"),
  zero: m.formatValue(0, ""),
  hash: hash,
  back: m.hiddenFrom(hash),
  cleared: m.hashFor({}, "#off=s3~s3_budget"),
  alpha: m.withAlpha("#0b5fa5", 0.55),
  passthrough: m.withAlpha("rgb(1, 2, 3)", 0.5),
  range: m.yRange([{values: [3, null, 5]}]),
  negative: m.yRange([{values: [-2, 4]}]),
  flat: m.yRange([{values: [0, null]}]),
}));
"""
    out = json.loads(subprocess.run([node, "-e", prog, str(SCRIPT)], capture_output=True,
                                    text=True, check=True).stdout)
    assert out["exact"] == "1.23456789 USD"
    assert out["gap"] == "not measured"
    assert out["zero"] == "0"
    assert out["hash"].startswith("#keep&off=")
    assert out["back"] == {"s3": {"s3_budget": True}, "a~b": {"x,y": True}}
    assert out["cleared"] == ""
    assert out["alpha"] == "rgba(11,95,165,0.55)"
    assert out["passthrough"] == "rgb(1, 2, 3)"
    assert out["range"][0] == 0 and out["range"][1] > 5
    assert out["negative"][0] < -2 and out["negative"][1] > 4
    assert out["flat"] == [0, 1.05]


def test_a_hostile_fragment_cannot_reach_a_prototype():
    """CodeQL js/prototype-polluting-assignment (code-scanning alert 6): the
    `#off=` fragment is reader-controlled, so a `__proto__` / `constructor` /
    `prototype` name is dropped, a malformed escape is ignored, and what is
    parsed lives in prototype-less maps."""
    node = shutil.which("node")
    assert node, "node is required to test charts.js (preinstalled on CI runners)"
    prog = r"""
const m = require(process.argv[1]);
const got = m.hiddenFrom("#off=__proto__~polluted,s3~__proto__,constructor~x," +
                         "prototype~y,a~constructor,%E0%A4%A~z,s3~ok");
console.log(JSON.stringify({
  polluted: ({}).polluted === undefined && Object.prototype.polluted === undefined,
  keys: Object.keys(got),
  s3: Object.keys(got.s3),
  nullProto: Object.getPrototypeOf(got) === null && Object.getPrototypeOf(got.s3) === null,
  missing: got.toString === undefined,
}));
"""
    out = json.loads(subprocess.run([node, "-e", prog, str(SCRIPT)], capture_output=True,
                                    text=True, check=True).stdout)
    assert out == {"polluted": True, "keys": ["s3"], "s3": ["ok"],
                   "nullProto": True, "missing": True}
