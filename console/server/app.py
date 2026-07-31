"""The HTTP application — router → index → render, over stdlib http.server.

There is no application state beyond the index, which is rebuilt from the
adapters on each pass (§5.6). A request is served by resolving its URL to a
view, then rendering that view from the current index — so a pasted URL on a
cold load reconstructs the view with nothing but the URL and the sources
(§3.2). This is the whole serving model; there is no session, no websocket,
no client-held state to lose.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from ..index.graph import Index
from ..render import html as render
from ..search.resolve import search
from .router import UnknownRoute, resolve


class ConsoleHandler(BaseHTTPRequestHandler):
    """Serves the four views. ``index`` is injected via the server object."""

    server: "ConsoleServer"

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parts = urlsplit(self.path)
        try:
            req = resolve(parts.path, parts.query)
        except UnknownRoute:
            self._send(404, "<h1>404</h1><p>No view at this URL.</p>")
            return

        index = self.server.index
        if req.view == "landing":
            body = render.landing_page(index)
        elif req.view == "list":
            body = render.list_page(index, req.kind, req.facets)
        elif req.view == "entity":
            ent = index.entity(req.entity_id)
            if ent is None:
                self._send(404, f"<h1>404</h1><p>No entity <code>{req.entity_id}</code>.</p>")
                return
            body = render.entity_page(index, ent)
        elif req.view == "search":
            body = render.search_page(search(index, req.query or ""), req.query or "")
        else:  # pragma: no cover - resolve() constrains the view set
            self._send(404, "<h1>404</h1>")
            return
        self._send(200, body)

    def _send(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # keep stdout clean; telemetry is §9
        pass


class ConsoleServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer carrying the entity index."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], index: Index):
        super().__init__(address, ConsoleHandler)
        self.index = index


def serve(index: Index, host: str = "127.0.0.1", port: int = 5180) -> ConsoleServer:
    """Build the server. Loopback is the default and is what makes the absence
    of authentication correct rather than negligent (config.example.yaml); a
    routable bind belongs behind an edge proxy, per §1.2."""
    return ConsoleServer((host, port), index)
