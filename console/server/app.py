"""The HTTP application — resolve once, render twice (§3.2, §3.8).

There is no application state beyond the index, which is rebuilt from the
adapters (§5.6). A request is served by resolving its URL to a view, then
rendering that view from the current index — so a pasted URL on a cold load
reconstructs the view with nothing but the URL and the sources (§3.2). No
session, no websocket, no client-held state to lose.

**The resolver runs once and both representations render from its result.**
That is §3.8 made structural rather than promised: there is one router and one
query, so the JSON and the HTML cannot drift in coverage. A route that exists
serves both; a route that does not exist 404s in both; and adding a route
cannot add it to one and forget the other, because neither has a route table
of its own.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlsplit

from .. import demo as demo_mode
from ..index import pages, planner, reports
from ..index.graph import Index
from ..render import html as render_html
from ..render import json as render_json
from .negotiate import negotiate
from .router import UnknownRoute, resolve


#: The only files served from `console/static/`, by exact path.
_STATIC = {
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/console.js": ("console.js", "text/javascript; charset=utf-8"),
    "/charts.js": ("charts.js", "text/javascript; charset=utf-8"),
    # uPlot 1.6.32 (MIT), vendored verbatim from the npm tarball and pinned by
    # digest in tests/test_interactive_charts.py: same-origin, no CDN, no build
    # step (alpha-engine-config-I12783, plan §3). Its licence ships beside it.
    "/vendor/uplot/uPlot.iife.min.js": ("vendor/uplot/uPlot.iife.min.js",
                                        "text/javascript; charset=utf-8"),
    "/vendor/uplot/uPlot.min.css": ("vendor/uplot/uPlot.min.css", "text/css; charset=utf-8"),
    "/vendor/uplot/LICENSE": ("vendor/uplot/LICENSE", "text/plain; charset=utf-8"),
}


#: `/actions/<issue number>` and `/ticks/<list>` — the only POST routes
#: (`ConsoleHandler.do_POST`).
_ACTION_POST = re.compile(r"^/actions/(?P<n>[1-9][0-9]{0,8})$")
_TICK_POST = re.compile(r"^/ticks/(?P<name>[a-z0-9][a-z0-9-]{0,39})$")
#: Where a tick returns the reader: a declared page, never another host.
_TICK_BACK = re.compile(r"^/page/[a-z0-9-]+(?:\?[^\s]*)?$")
#: Who ticked, as the edge proxy (Cloudflare Access) asserts it. Absent on a
#: loopback console with no proxy, where the comment says so instead.
_ACTOR_HEADER = "Cf-Access-Authenticated-User-Email"


def _same_origin(headers) -> bool:
    """True when the browser says the POST came from a page on this host."""
    host = (headers.get("Host") or "").strip().lower()
    source = headers.get("Origin") or headers.get("Referer") or ""
    if not host or not source or source == "null":
        return False
    return urlsplit(source).netloc.lower() == host


def _html_page(page: str) -> str:
    return render_html.with_scrolling_tables(render_html.with_site_nav(page))


class ConsoleHandler(BaseHTTPRequestHandler):
    """Serves the four views in two representations. ``index`` is on the server."""

    server: "ConsoleServer"

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parts = urlsplit(self.path)
        if parts.path in _STATIC:
            name, content_type = _STATIC[parts.path]
            # `no-cache` = revalidate before reuse: a stylesheet held past a
            # deploy is how a fixed layout kept rendering broken (2026-11-04).
            self._send(200, (Path(__file__).parent.parent / "static" / name).read_text(),
                       content_type, cache_control="no-cache")
            return
        # Captured once: the freshness metas (`_build_info`) must describe the
        # index this response renders, not one a refresh swapped in mid-render.
        index = self._served_index = self.server.index
        if parts.path == demo_mode.PREFIX or parts.path.startswith(demo_mode.PREFIX + "/"):
            self._demo(index, parts.path[len(demo_mode.PREFIX):] or "/", parts.query)
            return
        path, as_json = negotiate(
            parts.path,
            self.headers.get("Accept"),
            lambda p: _resolves(index, p, parts.query),
            lambda p: _routable(p, parts.query),
        )

        try:
            req = resolve(path, parts.query)
        except UnknownRoute:
            self._fail(404, as_json, f"No view at {path}.")
            return

        if req.view in {"entity", "history"} and index.entity(req.entity_id or "") is None:
            self._fail(404, as_json, f"No entity {req.entity_id}.")
            return
        if req.view == "planner-project" and planner.project(index, req.project_id or "") is None:
            self._fail(404, as_json, f"No planner project {req.project_id}.")
            return
        if (req.view == "reports" and req.feed_id is not None
                and reports.feed(index, req.feed_id) is None):
            self._fail(404, as_json, f"No report feed {req.feed_id}.")
            return
        if req.view == "page" and pages.page(index, req.page_id) is None:
            self._fail(404, as_json, f"No page {req.page_id}.")
            return
        if req.view == "registry" and req.registry_name not in index.registry_names():
            self._fail(404, as_json, f"No registry {req.registry_name}.")
            return

        if as_json:
            try:
                body = render_json.dumps(render_json.payload(index, req))
            except (TypeError, ValueError) as exc:
                # A value the wire cannot spell is a defect in the source or
                # the serializer — answered as a 500 IN JSON so the reader
                # (and the deploy prove) sees the failure named, instead of a
                # dropped connection that reads as an empty index.
                self.log_error("json render failed for %s: %s", path, exc)
                self._send(
                    500,
                    render_json.dumps({"schema_version": render_json.SCHEMA_VERSION,
                                       "error": "render_failed",
                                       "detail": f"{exc.__class__.__name__}: {exc}"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send(200, body, "application/json; charset=utf-8")
            return
        self._send(200, _html(index, req), "text/html; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        """The console's two writes: tick or untick an action
        (`index/actions.py`), or mark a row on a declared tick list
        (`index/ticks.py`, saved to that list's own S3 object). Every other
        path answers 405, so no other view can grow a write by accident.

        Same-origin is required: the browser's ``Origin`` (or, failing that,
        ``Referer``) must name this request's own ``Host``, so a page on
        another site cannot post a tick through the reader's signed-in
        session. The edge proxy's identity header names who ticked."""
        parts = urlsplit(self.path)
        m = _ACTION_POST.match(parts.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 4096)).decode("utf-8", "replace") if length else ""
        tm = _TICK_POST.match(parts.path)
        if m is None and tm is None:
            self._send(405, "<h1>405</h1><p>Only an action tick or a list tick is writable.</p>",
                       "text/html; charset=utf-8")
            return
        if not _same_origin(self.headers):
            self._send(403, "<h1>403</h1><p>A tick must come from this console's own page.</p>",
                       "text/html; charset=utf-8")
            return
        form = dict(parse_qsl(raw, keep_blank_values=True))
        done = form.get("done", "1") != "0"
        actor = (self.headers.get(_ACTOR_HEADER) or "").strip() or None
        index = self._served_index = self.server.index
        if tm is not None:
            return self._tick(index, tm.group("name"), form, done, actor)
        from ..index import actions

        number = int(m.group("n"))
        try:
            act = actions.tick(index, number, done, actor)
        except actions.TickRefused as exc:
            self._send(exc.status, _html_page(render_html.actions_page(
                index, notice=f"Not saved: {exc}")), "text/html; charset=utf-8")
            return
        self.send_response(303)
        self.send_header("Location", f"/actions#a{act.number}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _tick(self, index: Index, name: str, form: dict, done: bool,
              actor: str | None) -> None:
        """A list tick (`index/ticks.py`): mark one shown row, then return the
        reader to the page it came from."""
        from ..index import ticks
        from ..index.build import now_iso

        back = form.get("back") or ""
        back = back if _TICK_BACK.match(back) else "/"
        try:
            ticks.tick(index, name, form.get("key") or "", done, actor, now_iso(),
                       allowed=pages.tick_keys(index, name))
        except ticks.TickRefused as exc:
            self._send(exc.status, f"<h1>{exc.status}</h1><p>Not saved: {render_html.esc(str(exc))}</p>"
                       f'<p><a href="{render_html.esc(back)}">back</a></p>', "text/html; charset=utf-8")
            return
        self.send_response(303)
        self.send_header("Location", back)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _demo(self, index: Index, path: str, query: str) -> None:
        """A `/demo/...` request (alpha-engine-config-I13023; `console/demo.py`).

        Off unless the config enables it: a disabled demo is a plain 404, the
        same answer as a route that does not exist. Enabled, only the tour and
        its declared pages are served — in both representations, negotiated
        exactly as every other route is (§3.8) — and every other path answers
        "not part of the demo". Every byte passes `demo.finish` (HTML) or is
        built by `demo.payload` (JSON), which redact it."""
        demo = demo_mode.declared(index)

        def target(p: str):
            try:
                req = resolve(p, query)
            except UnknownRoute:
                return None
            return req if demo_mode.resolved(demo, index, req) else None

        path, as_json = negotiate(
            path, self.headers.get("Accept"),
            lambda p: demo.enabled and target(p) is not None,
            lambda p: demo.enabled and _routable(p, query))
        req = target(path) if demo.enabled else None
        if req is None:
            message = ("This is not part of the demo." if demo.enabled
                       else f"No view at {demo_mode.PREFIX}{path}.")
            if as_json or not demo.enabled:
                self._fail(404, as_json, message)
                return
            self._send(404, demo_mode.finish(
                f"<!doctype html><html><head><title>{render_html.esc(demo.title)}</title>"
                f"</head><body><h1>Not shown</h1><p>{message} "
                f'<a href="/">Back to the tour</a></p></body></html>', demo),
                "text/html; charset=utf-8")
            return
        if as_json:
            self._send(200, render_json.dumps(demo_mode.payload(
                index, demo, req, render_json.SCHEMA_VERSION)),
                "application/json; charset=utf-8")
            return
        if req.view == "landing":
            body = render_html.demo_tour_page(index, demo)
        else:
            body = render_html.page_page(demo_mode.view(index), req.page_id or "",
                                         dict(req.params), demo=demo)
        body = render_html.with_scrolling_tables(render_html.with_demo_nav(body, demo))
        self._send(200, demo_mode.finish(body, demo), "text/html; charset=utf-8")

    def _fail(self, status: int, as_json: bool, message: str) -> None:
        """A 404 in the representation that was asked for.

        An agent that requested JSON and got an HTML error page has to parse
        prose to find out what happened, which is how a missing entity becomes
        an unhandled exception three layers up.
        """
        if as_json:
            body = render_json.dumps(
                {"schema_version": render_json.SCHEMA_VERSION,
                 "error": "not_found", "detail": message},
            )
            self._send(status, body, "application/json; charset=utf-8")
            return
        self._send(status, f"<h1>{status}</h1><p>{render_html.esc(message)}</p>",
                   "text/html; charset=utf-8")

    def _build_info(self):
        """The served index's build info for the live-freshness metas, or None
        when there is no index to ask (an error page served before the first
        build, or a handler built without a server): the metas then say the
        data time is unknown rather than failing the response."""
        try:
            served = getattr(self, "_served_index", None)
            return (served if served is not None else self.server.index).build_info
        except AttributeError:
            return None

    def _send(self, status: int, body: str, content_type: str,
              cache_control: str | None = None) -> None:
        if content_type.startswith("text/html"):
            body = body.replace(
                "<head>", "<head>" + render_html.head_inject(self._build_info()), 1
            )
        payload = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            if cache_control:
                self.send_header("Cache-Control", cache_control)
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError) as exc:
            # The client hung up before the response finished writing — a
            # `box_health.sh` probe that gave up at its 3s timeout while a
            # rebuild held the GIL (alpha-engine-config-I10615) is the
            # measured case, but any reader that disconnects early lands
            # here. The response was built correctly; there is simply
            # nowhere left to deliver it, which is not a server fault, so
            # this is one line — never the full traceback the socketserver
            # machinery would otherwise print to the journal on every
            # abandoned probe.
            self.log_error(
                "client disconnected during response to %s (%s): %s",
                self.path, type(exc).__name__, exc,
            )

    def log_message(self, *args) -> None:  # keep stdout clean; telemetry is §9
        pass


def _html(index: Index, req) -> str:
    """The HTML rendering of an already-resolved request.

    Mirrors `render_json.payload` exactly — same views, same order, no view in
    one that is absent from the other. If a view is ever added to one of these
    and not the other, `test_every_route_serves_both_representations` fails,
    because it enumerates routes rather than listing them by hand.
    """
    return render_html.with_scrolling_tables(
        render_html.with_site_nav(_page(index, req)))


def _page(index: Index, req) -> str:
    if req.view == "landing":
        return render_html.landing_page(index)
    if req.view == "dashboards":
        return render_html.dashboards_page(index, req.query)
    if req.view == "health":
        return render_html.health_page(index)
    if req.view == "list":
        return render_html.list_page(index, req.kind, req.facets, req.page,
                                     all_runs=req.all_runs, days=req.days,
                                     days_all=req.days_all)
    if req.view == "entity":
        return render_html.entity_page(index, index.entity(req.entity_id),
                                       all_runs=req.all_runs)
    if req.view == "history":
        return render_html.history_page(index, index.entity(req.entity_id), req.window_hours or 24)
    if req.view == "doctor":
        return render_html.doctor_page(index, req.query or "")
    if req.view == "planner":
        return render_html.planner_page(index)
    if req.view == "planner-project":
        return render_html.planner_project_page(index, req.project_id or "")
    if req.view == "reports":
        return render_html.reports_page(index, req.feed_id)
    if req.view == "exceptions":
        return render_html.exceptions_page(index)
    if req.view == "actions":
        return render_html.actions_page(index)
    if req.view == "page":
        return render_html.page_page(index, req.page_id or "", dict(req.params))
    if req.view == "registry":
        return render_html.registry_page(index, req.registry_name or "",
                                         all_runs=req.all_runs)
    return render_html.search_page(index, req.query or "", all_runs=req.all_runs)


def _routable(path: str, query: str) -> bool:
    """Whether this path names a known ROUTE, regardless of whether the entity
    behind it exists. The weaker half of negotiation — see `negotiate()`."""
    try:
        resolve(path, query)
    except UnknownRoute:
        return False
    return True


def _resolves(index: Index, path: str, query: str) -> bool:
    """Whether this path names something that exists.

    Consulted by content negotiation BEFORE the `.json` suffix is considered,
    so an artifact whose object key ends in `.json` stays addressable at its
    own identifier (§3.2 — the identifier IS the URL).
    """
    try:
        req = resolve(path, query)
    except UnknownRoute:
        return False
    if req.view == "entity":
        return index.entity(req.entity_id or "") is not None
    if req.view == "planner-project":
        return planner.project(index, req.project_id or "") is not None
    if req.view == "reports" and req.feed_id is not None:
        return reports.feed(index, req.feed_id) is not None
    if req.view == "page":
        return pages.page(index, req.page_id) is not None
    return True


class ConsoleServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer carrying the entity index."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], source):
        super().__init__(address, ConsoleHandler)
        # Either an Index or a Supervisor. A Supervisor hands back whichever
        # index is current at the moment the request is served, so a rebuild is
        # invisible to an in-flight request and a half-built graph is never
        # rendered (§5.9).
        self._source = source

    @property
    def index(self) -> Index:
        source = self._source
        return source.current if hasattr(source, "current") else source


def serve(source, host: str = "127.0.0.1", port: int = 5180) -> ConsoleServer:
    """Build the server. Loopback is the default and is what makes the absence
    of authentication correct rather than negligent (config.example.yaml); a
    routable bind belongs behind an edge proxy, per §1.2 — and that proxy's
    identity covers the JSON representation too, since §3.8 forbids a second
    authentication story for it.

    ``source`` is an Index or a Supervisor; with a Supervisor the served index
    is re-read per request, which is what makes the §5.9 swap atomic from a
    handler's point of view."""
    return ConsoleServer((host, port), source)
