"""alpha-engine-config-I10615: a client that disconnects mid-response is not
a server fault.

`box_health.sh`'s 3s HTTP probe gives up whenever the landing page is slow
(historically: a rebuild in flight sharing the GIL). The abandoned write
raised `BrokenPipeError`/`ConnectionResetError` straight out of
`ConsoleHandler._send`, which the stdlib socketserver machinery then printed
as a full traceback to the journal — several per hour on the box. These
assert `_send` catches both, logs exactly one line via `self.log_error`, and
never lets the exception escape.
"""
from __future__ import annotations

import pytest

from console.server.app import ConsoleHandler


class _RaisingWfile:
    """A write-side that behaves like a socket whose reader has gone away."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def write(self, data: bytes) -> int:
        raise self._exc


def _bare_handler(wfile) -> ConsoleHandler:
    """A `ConsoleHandler` with none of `BaseHTTPRequestHandler.__init__`'s
    socket setup run — `_send` only needs `wfile`, `path`, `requestline` and
    `request_version` for the response-line machinery it calls into."""
    handler = object.__new__(ConsoleHandler)
    handler.wfile = wfile
    handler.path = "/"
    handler.requestline = "GET / HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.close_connection = True
    return handler


@pytest.mark.parametrize("exc_type", [BrokenPipeError, ConnectionResetError])
def test_send_logs_one_line_and_does_not_raise(exc_type):
    handler = _bare_handler(_RaisingWfile(exc_type("client gone")))
    logged: list[tuple] = []
    handler.log_error = lambda *args: logged.append(args)

    # No exception should escape — this is the fix: previously the raw
    # BrokenPipeError propagated to the socketserver's default error handler,
    # which is what printed the traceback into the journal.
    handler._send(200, "<html><head></head></html>", "text/html; charset=utf-8")

    assert len(logged) == 1
    fmt, path, exc_name, exc = logged[0]
    assert path == "/"
    assert exc_name == exc_type.__name__


def test_send_succeeds_normally_when_the_client_is_still_there():
    written = []

    class _OkWfile:
        def write(self, data: bytes) -> int:
            written.append(data)
            return len(data)

    handler = _bare_handler(_OkWfile())
    logged: list[tuple] = []
    handler.log_error = lambda *args: logged.append(args)

    handler._send(200, "<html><head></head><body>hi</body></html>",
                  "text/html; charset=utf-8")

    assert logged == []
    assert b"".join(written).endswith(b"hi</body></html>")
