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

import json as jsonlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from ..index.graph import Index
from ..render import html as render_html
from ..render import json as render_json
from ..search.resolve import search
from .negotiate import negotiate
from .router import UnknownRoute, resolve


class ConsoleHandler(BaseHTTPRequestHandler):
    """Serves the four views in two representations. ``index`` is on the server."""

    server: "ConsoleServer"

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parts = urlsplit(self.path)
        index = self.server.index
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

        if req.view == "entity" and index.entity(req.entity_id or "") is None:
            self._fail(404, as_json, f"No entity {req.entity_id}.")
            return
        if req.view == "registry" and not any(a.name == req.registry_name for a in index.build_info.adapters):
            self._fail(404, as_json, f"No registry {req.registry_name}.")
            return

        if as_json:
            self._send(
                200,
                jsonlib.dumps(render_json.payload(index, req), indent=2,
                              sort_keys=True),
                "application/json; charset=utf-8",
            )
            return
        self._send(200, _html(index, req), "text/html; charset=utf-8")

    def _fail(self, status: int, as_json: bool, message: str) -> None:
        """A 404 in the representation that was asked for.

        An agent that requested JSON and got an HTML error page has to parse
        prose to find out what happened, which is how a missing entity becomes
        an unhandled exception three layers up.
        """
        if as_json:
            body = jsonlib.dumps(
                {"schema_version": render_json.SCHEMA_VERSION,
                 "error": "not_found", "detail": message},
                indent=2, sort_keys=True,
            )
            self._send(status, body, "application/json; charset=utf-8")
            return
        self._send(status, f"<h1>{status}</h1><p>{render_html.esc(message)}</p>",
                   "text/html; charset=utf-8")

    def _send(self, status: int, body: str, content_type: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # keep stdout clean; telemetry is §9
        pass


def _html(index: Index, req) -> str:
    """The HTML rendering of an already-resolved request.

    Mirrors `render_json.payload` exactly — same views, same order, no view in
    one that is absent from the other. If a view is ever added to one of these
    and not the other, `test_every_route_serves_both_representations` fails,
    because it enumerates routes rather than listing them by hand.
    """
    if req.view == "landing":
        return render_html.landing_page(index)
    if req.view == "list":
        return render_html.list_page(index, req.kind, req.facets, req.page)
    if req.view == "entity":
        return render_html.entity_page(index, index.entity(req.entity_id))
    if req.view == "doctor":
        return render_html.doctor_page(index, req.query or "")
    if req.view == "registry":
        return render_html.registry_page(index, req.registry_name or "")
    return render_html.search_page(search(index, req.query or ""), req.query or "")


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
