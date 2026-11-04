"""Demo mode (`/demo/...`, alpha-engine-config-I13023) — the hidden classes
never render.

A guided demo reuses the declared pages and sources and only decides what is
shown. These tests serve one index both ways and assert, over the served
bytes, that: demo mode is off unless declared; only the tour and the declared
page set are served (in both representations, contract § API); hidden rows, facet values and fields do not
exist in the demo; every built-in redaction class is redacted whatever a
source wrote; every internal link stays inside the prefix; and the numbers a
demo page shows are the same evaluation as the operator's page.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request

import pytest

from console import demo
from console.index import pages
from console.index.build import BuildInfo
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus
from console.model.kinds import Kind, State
from console.render import html as render_html
from console.server.app import serve

#: One sample of every class that must never reach an audience, planted in a
#: VISIBLE row's text field and source, so only redaction can stop it. The
#: credential-shaped samples are split literals, so a secret scanner reading
#: this file sees no credential — they exist only at run time.
SECRETS = {
    "ARN": "arn:aws:iam::123456789012:role/example-role",
    "account id": "210987654321",
    "access key": "AKIA" + "ABCDEFGHIJKLMNOP",
    "token": "ghp" + "_abcdefghijklmnopqrstuvwxyz0123456789",
    "credential": "pass" + "word=hunter2hunter2",
    "e-mail": "someone@example.org",
    "storage path": "s3://private-bucket/strategy/weights.json",
    "instance id": "i-0123456789abcdef0",
    "IP address": "10.20.30.40",
    "private key": "-----BEGIN RSA " + "PRIVATE KEY-----MIIEow-----END RSA " + "PRIVATE KEY-----",
    "declared prefix": "/example-org/telegram/BOT_TOKEN",
}
LEAKY_NOTE = " · ".join(SECRETS.values())


def _run(rid, facets, fields, label, state=State.HEALTHY,
         source="s3://private-bucket/rows.json"):
    return Entity(
        kind=Kind.RUN, id=rid, state=state,
        provenance=Provenance(source, "2026-11-01T12:00:00+00:00",
                              "https://github.com/example-org/example-repo/issues/1"),
        facets=facets, detail={"label": label, "fields": fields})


ROWS = (
    _run("arena:2026-10-30", {"area": "crucible", "owner": "harness"},
         {"promoted": {"value": 1, "render": "value"},
          "challengers": {"value": 4, "render": "value"},
          "cost_usd": {"value": 987.65, "unit": "USD", "render": "value"},
          "note": {"value": LEAKY_NOTE, "render": "value"}},
         "R slot promoted a challenger"),
    # Hidden by facet value: spend.
    _run("spend:aws", {"area": "crucible", "pane": "cost", "owner": "finance"},
         {"promoted": {"value": 0, "render": "value"}},
         "ZZ-SPEND-ROW-LABEL", state=State.FAILED),
    # Hidden by facet value: a personal portfolio.
    _run("portfolio:holdings", {"area": "crucible", "owner": "personal-portfolio"},
         {"promoted": {"value": 0, "render": "value"}},
         "ZZ-PORTFOLIO-ROW-LABEL", state=State.DEGRADED),
    # Hidden by kind.
    Entity(kind=Kind.INCIDENT, id="sev2:ZZ-OPS-INCIDENT", state="open",
           provenance=Provenance("tracker", "2026-11-01T12:00:00+00:00"),
           facets={"area": "crucible"}, detail={"label": "ZZ-OPS-INCIDENT"}),
)

PAGES = {
    "demo-harness": {
        "title": "The harness",
        "question": "What did the last experiment decide?",
        "variables": ["owner"],
        "links": [{"title": "Evidence", "url": "https://github.com/example-org/example-repo/issues/1"},
                  {"title": "Every run", "url": "/run?area=crucible"}],
        "sections": [
            {"type": "text", "title": "What this is",
             "body": "Four slots, each with an **incumbent** and challengers."},
            {"type": "tiles", "tiles": [
                {"label": "Promoted", "select": {"kind": "run", "facets": {"area": "crucible"}},
                 "field": "promoted"},
                {"label": "Spend", "select": {"kind": "run", "facets": {"area": "crucible"}},
                 "field": "cost_usd"}]},
            {"type": "table", "title": "Arena cycles",
             "select": {"facets": {"area": "crucible"}},
             "fields": ["promoted", "challengers", "cost_usd", "note"]},
            {"type": "exceptions", "title": "Open faults",
             "select": {"facets": {"area": "crucible"}}},
        ],
    },
    "demo-next": {
        "title": "What is next", "question": "What has it not shown yet?",
        "sections": [{"type": "text", "body": "Paper weeks before real capital."}],
    },
    "ops-internal": {
        "title": "ZZ-INTERNAL-PAGE", "question": "Internal only?",
        "sections": [{"type": "table", "title": "All",
                      "select": {"facets": {"area": "crucible"}}}],
    },
}

DEMO = {
    "enabled": True,
    "title": "Guided tour",
    "intro": "A few pages; each links to its evidence.",
    "pages": ["demo-harness", "demo-*"],
    "hide": {"kinds": ["incident"],
             "facets": {"pane": ["cost"], "owner": ["personal-portfolio"]},
             "fields": ["cost_usd"]},
    "redact": {"prefixes": ["/example-org/"]},
}

HIDDEN_TEXT = ("ZZ-SPEND-ROW-LABEL", "spend:aws", "ZZ-PORTFOLIO-ROW-LABEL",
               "portfolio:holdings", "personal-portfolio", "ZZ-OPS-INCIDENT",
               "cost_usd", "987.65", "ZZ-INTERNAL-PAGE", "private-bucket")


def _index(demo_decl=DEMO) -> Index:
    idx = Index()
    idx.add_result(AdapterResult(name="zz-internal-adapter", status=AdapterStatus.OK,
                                 entities=ROWS))
    idx.build_info = BuildInfo(built_at="2026-11-01T12:05:00Z")
    declared = pages.parse(PAGES)
    pages.attach(idx, declared)
    demo.attach(idx, demo.parse(demo_decl, page_ids={p.id for p in declared}))
    return idx


def _serve(idx):
    server = serve(idx, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture()
def live():
    server, base = _serve(_index())
    yield base
    server.shutdown()
    server.server_close()


@pytest.fixture()
def live_disabled():
    server, base = _serve(_index({**DEMO, "enabled": False}))
    yield base
    server.shutdown()
    server.server_close()


def _get(url: str, accept: str | None = None) -> tuple[int, str, str]:
    req = urllib.request.Request(url, headers={"Accept": accept} if accept else {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers["Content-Type"], resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.headers["Content-Type"], err.read().decode()


# ------------------------------------------------------------ off by default --

def test_absent_block_is_disabled():
    assert demo.parse(None) is demo.DISABLED
    assert demo.parse(None).enabled is False


def test_disabled_demo_is_a_plain_404_and_changes_nothing_else(live_disabled):
    for path in ("/demo", "/demo/", "/demo/page/demo-harness"):
        status, _, body = _get(live_disabled + path)
        assert status == 404, path
        assert "The harness" not in body
    status, _, body = _get(live_disabled + "/page/demo-harness")
    assert status == 200 and "987.65" in body  # the operator's page is untouched


# -------------------------------------------------------------- default deny --

@pytest.mark.parametrize("path", [
    "/demo/page/ops-internal", "/demo/run", "/demo/run/arena:2026-10-30",
    "/demo/search?q=spend", "/demo/reports", "/demo/planner", "/demo/exceptions",
    "/demo/dashboards", "/demo/doctor/spend:aws",
    "/demo/page/no-such-page", "/demo/history/run/arena:2026-10-30",
])
def test_only_the_tour_and_declared_pages_are_served(live, path):
    status, ctype, body = _get(live + path)
    assert status == 404, path
    assert ctype.startswith("text/html")
    assert "not part of the demo" in body
    for text in HIDDEN_TEXT:
        assert text not in body, (path, text)


@pytest.mark.parametrize("how", ["accept", "suffix"])
def test_json_is_the_same_filtered_view_at_the_same_url(live, how):
    url, accept = ((live + "/demo/page/demo-harness", "application/json") if how == "accept"
                   else (live + "/demo/page/demo-harness.json", None))
    status, ctype, body = _get(url, accept=accept)
    assert status == 200 and ctype.startswith("application/json")
    doc = json.loads(body)
    assert doc["demo"] is True and doc["schema_version"]
    assert [p["id"] for p in doc["pages"]] == ["demo-harness", "demo-next"]
    for text in HIDDEN_TEXT + ("zz-internal-adapter",):
        assert text not in body, text
    for label, sample in SECRETS.items():
        assert json.dumps(sample)[1:-1] not in body, label
    urls = re.findall(r'"url": ("[^"]*"|null)', body)
    assert urls and all(u == "null" or u.startswith('"/demo/') or u.startswith('"https://')
                        for u in urls), urls
    table = doc["page"]["sections"][2]
    assert [r["fields"] for r in table["rows"]][0]["promoted"] == 1


@pytest.mark.parametrize("path", ["/demo/run", "/demo/page/ops-internal.json",
                                  "/demo/search.json?q=spend"])
def test_a_refused_route_answers_404_in_json_when_json_was_asked(live, path):
    status, ctype, body = _get(live + path, accept="application/json")
    assert status == 404 and ctype.startswith("application/json")
    assert json.loads(body)["error"] == "not_found"
    assert "ZZ-INTERNAL-PAGE" not in body


def test_a_disabled_demo_404s_in_json_too(live_disabled):
    status, ctype, _ = _get(live_disabled + "/demo/page/demo-harness", accept="application/json")
    assert status == 404 and ctype.startswith("application/json")


def test_tour_json(live):
    status, _, body = _get(live + "/demo/", accept="application/json")
    doc = json.loads(body)
    assert status == 200 and doc["view"] == "tour"
    assert [p["url"] for p in doc["pages"]] == ["/demo/page/demo-harness", "/demo/page/demo-next"]


def test_tour_lists_the_declared_pages_in_order(live):
    status, _, body = _get(live + "/demo/")
    assert status == 200
    assert "A few pages; each links to its evidence." in body
    harness = body.index('href="/demo/page/demo-harness"')
    nxt = body.index('href="/demo/page/demo-next"')
    assert harness < nxt
    assert "ops-internal" not in body and "ZZ-INTERNAL-PAGE" not in body
    assert "demo view" in body  # the banner says what the reader is looking at


# ------------------------------------------------- the hidden classes never render --

def test_demo_page_never_renders_a_hidden_class(live):
    status, _, body = _get(live + "/demo/page/demo-harness")
    assert status == 200
    assert "R slot promoted a challenger" in body
    for text in HIDDEN_TEXT:
        assert text not in body, text
    for label, sample in SECRETS.items():
        assert sample not in body, label
    # The per-source read list is operator topology; a demo says only when.
    assert "zz-internal-adapter" not in body
    assert "data as of" in body


def test_a_variable_picker_offers_no_hidden_value(live):
    _, _, body = _get(live + "/demo/page/demo-harness?owner=personal-portfolio")
    assert "ZZ-PORTFOLIO-ROW-LABEL" not in body
    options = re.findall(r'<option value="([^"]*)"', body)
    assert "harness" in options and "finance" not in options


def test_every_internal_link_stays_inside_the_demo(live):
    for path in ("/demo/", "/demo/page/demo-harness", "/demo/page/ops-internal"):
        _, _, body = _get(live + path)
        for url in re.findall(r'(?:href|action)="(/[^"]*)"', body):
            assert url.startswith("/demo/") or url == "/styles.css", (path, url)
    _, _, body = _get(live + "/demo/page/demo-harness")
    # Row and tile links point at entity pages, which the demo does not serve:
    # they render as text, not as links that dead-end.
    assert "/run/" not in body
    assert 'href="https://github.com/example-org/example-repo/issues/1"' in body
    assert "Every run" in body and 'href="/demo/run' not in body


def test_text_sections_render_as_markdown(live):
    _, _, body = _get(live + "/demo/page/demo-harness")
    assert "<strong>incumbent</strong>" in body


def test_numbers_are_the_same_evaluation_in_both_views():
    idx = _index()
    found = pages.page(idx, "demo-harness")
    full = pages.evaluate(idx, found, {})
    shown = pages.evaluate(demo.view(idx), found, {})
    full_row = next(r for r in full["sections"][2]["rows"] if r["id"] == "arena:2026-10-30")
    demo_row = next(r for r in shown["sections"][2]["rows"] if r["id"] == "arena:2026-10-30")
    for name in ("promoted", "challengers"):
        assert demo_row["fields"][name] == full_row["fields"][name]
    assert demo_row["fields"]["cost_usd"] is None  # hidden, never altered
    assert {r["id"] for r in shown["sections"][2]["rows"]} == {"arena:2026-10-30"}


# --------------------------------------------------------------- redaction --

@pytest.mark.parametrize("label", sorted(SECRETS))
def test_every_builtin_class_is_redacted(label):
    d = demo.parse({"enabled": False, "redact": {"prefixes": ["/example-org/"]}})
    out = d.redact(f"<td>before {SECRETS[label]} after</td>")
    assert SECRETS[label] not in out
    assert out.startswith("<td>before ") and out.endswith(" after</td>")


@pytest.mark.parametrize("text", [
    "2026-11-01T12:05:00Z", "1,234.56 USD", "v2 rank-IC -0.031", "+0.02% vs SPY",
    '<a href="/demo/page/demo-harness">The harness</a>', "4 of 12 challengers",
])
def test_redaction_leaves_ordinary_text_alone(text):
    assert demo.DISABLED.redact(text) == text


def test_declared_patterns_add_to_the_builtin_classes():
    d = demo.parse({"redact": {"patterns": [r"ACME-\d+"]}})
    assert d.redact("ACME-42 and 123456789012") == "[hidden] and [account id hidden]"


# ------------------------------------------------------------- declaration --

@pytest.mark.parametrize("raw, message", [
    ("yes", "must be a mapping"),
    ({"enabled": "true", "pages": ["demo-harness"]}, "`enabled` must be true or false"),
    ({"enabled": True}, "at least one page"),
    ({"pages": ["no-such-*"]}, "matches no declared page"),
    ({"hide": {"facet": {"pane": ["cost"]}}}, "unknown key(s) facet"),
    ({"hidden": {}}, "unknown key(s) hidden"),
    ({"redact": {"patterns": ["("]}}, "is not a regex"),
    ({"hide": {"facets": {"pane": "cost"}}}, "must be a list"),
    ({"title": ""}, "`title` must be non-empty"),
    ({"links": {"external": []}}, "must name at least one pattern"),
    ({"links": {"external": ["("]}}, "is not a regex"),
    ({"links": {"internal": ["x"]}}, "unknown key(s) internal"),
    ({"links": "x"}, "`links` must be a mapping"),
])
def test_a_bad_declaration_fails_the_build_by_name(raw, message):
    with pytest.raises(demo.DemoConfigError, match=re.escape(message)):
        demo.parse(raw, page_ids={"demo-harness"})


def test_a_text_section_needs_a_body_and_takes_no_empty():
    with pytest.raises(pages.PagesConfigError, match="`body` must be non-empty"):
        pages.parse({"p": {"title": "P", "question": "Q?",
                           "sections": [{"type": "text", "body": "  "}]}})
    with pytest.raises(pages.PagesConfigError, match="`empty` is for selector sections"):
        pages.parse({"p": {"title": "P", "question": "Q?",
                           "sections": [{"type": "text", "body": "x", "empty": "none"}]}})


def test_operator_page_renders_text_sections_too():
    idx = _index()
    out = render_html.page_page(idx, "demo-next")
    assert "Paper weeks before real capital." in out


# ------------------------------------- Codex review 2026-11-03 (0afbd50e) --

def test_a_relative_link_cannot_escape_the_demo():
    d = demo.parse({"pages": ["demo-harness"]}, {"demo-harness"})
    out = demo.finish('<p><a href="../../run">All runs</a> and '
                      '<a href="run">here</a></p>', d)
    assert "href" not in out
    assert out.count('class="demo-unlinked"') == 2


def test_external_and_declared_links_are_kept():
    d = demo.parse({"pages": ["demo-harness"]}, {"demo-harness"})
    out = demo.finish('<a href="https://nousergon.ai/x">site</a> '
                      '<a href="#top">top</a> <a href="/page/demo-harness">h</a> '
                      '<a href="//evil.example/run">x</a>', d)
    assert 'href="https://nousergon.ai/x"' in out
    assert 'href="#top"' in out
    assert f'href="{demo.PREFIX}/page/demo-harness"' in out
    assert 'href="//' not in out


def test_a_numeric_account_id_is_redacted_in_json():
    d = demo.parse({"pages": ["demo-harness"]}, {"demo-harness"})
    out = demo._scrub({"account": 210987654321, "count": 4, "ok": True}, d)
    assert out["account"] == "[account id hidden]"
    assert out["count"] == 4 and out["ok"] is True


def test_email_redaction_matches_the_regex_it_replaced():
    # The linear matcher must hide exactly what the old pattern hid,
    # including a second address that starts right after the first ends.
    import re
    old = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
    new = dict(demo.BUILTIN_REDACTIONS)["e-mail"]
    for text in ("someone@example.org", "a@b.com.x%y@c.org", "x@@y.co", "@a.bc",
                 "q.@w.er.ty@u.io z", "mail a.b+c@d-e.f.gh, then x@y", "no at sign"):
        assert new.sub("[e-mail hidden]", text) == old.sub("[e-mail hidden]", text), text


def test_redaction_and_link_scan_are_linear_on_hostile_bodies():
    # CodeQL py/polynomial-redos: a long run of letters with no `@` made the
    # e-mail regex quadratic (60 KB took ~6 s); repeated unclosed `<a ` made
    # the anchor scan quadratic. Both are linear now; the bound is generous.
    import time
    d = demo.DISABLED
    for body in ("a" * 300_000, "<a " * 100_000 + ">", "a@" * 150_000):
        started = time.monotonic()
        demo.finish(body, d)
        assert time.monotonic() - started < 3.0


def test_a_hidden_field_named_in_a_tiles_also_list_is_removed_by_name():
    # alpha-engine-config-I12783: `also:` names further fields; a hidden one
    # must not print even its name in the demo view.
    from console.demo import Demo

    demo = Demo(hide_fields=frozenset({"secret"}))
    page = {"sections": [{"type": "tiles", "tiles": [
        {"label": "x", "field": "mtd", "also": [
            {"field": "secret", "label": "secret", "text": "1", "unit": None},
            {"field": "vs_spy_pct", "label": "vs SPY", "text": "2", "unit": "%"}]}]}]}
    tiles = demo.page_doc(page)["sections"][0]["tiles"]
    assert [a["field"] for a in tiles[0]["also"]] == ["vs_spy_pct"]


# ------------------------------------------ links.external (I13023 polish) --

def test_undeclared_external_links_stay_links():
    d = demo.parse({"pages": ["demo-harness"]}, {"demo-harness"})
    assert d.external_links is None
    out = demo.finish('<a href="https://github.com/acme/private/issues/1">x</a>', d)
    assert 'href="https://github.com/acme/private/issues/1"' in out


def test_declared_external_links_keep_only_matches_in_html():
    d = demo.parse({"pages": ["demo-harness"],
                    "links": {"external": [r"https://github\.com/acme/public([/#?]|$)"]}},
                   {"demo-harness"})
    out = demo.finish('<a href="https://github.com/acme/public#readme">pub</a> '
                      '<a href="https://github.com/acme/public-ish/x">near</a> '
                      '<a href="https://github.com/acme/private/issues/1">priv</a> '
                      '<a href="#top">top</a> <a href="/page/demo-harness">h</a>', d)
    assert 'href="https://github.com/acme/public#readme"' in out
    assert 'href="https://github.com/acme/public-ish' not in out
    assert 'href="https://github.com/acme/private' not in out
    assert out.count('class="demo-unlinked"') == 2
    assert 'href="#top"' in out and f'href="{demo.PREFIX}/page/demo-harness"' in out


def test_declared_external_links_null_the_json_url():
    d = demo.parse({"pages": ["demo-harness"],
                    "links": {"external": [r"https://example\.org/"]}}, {"demo-harness"})
    out = demo._scrub({"links": [{"title": "a", "url": "https://example.org/x"},
                                 {"title": "b", "url": "https://elsewhere.example/x"},
                                 {"title": "c", "url": "#anchor"}]}, d)
    assert [link["url"] for link in out["links"]] == ["https://example.org/x", None, "#anchor"]
