"""One read-only SQLite connection, shared by every SQL path in the console.

nousergon-console#213 (audit PLAT-09): the `sql-source` driver and adapter
opened their database with a bare ``sqlite3.connect(path)``, whose default
mode is read-write-CREATE. A missing or mistyped source path therefore became
a new empty database, written by a console that promises it never persists or
mutates what it observes -- and the original "file is missing" diagnosis
turned into a confusing "no such table". The `sql-query` adapter already used
``mode=ro``; this module is that one capability, enforced the same way for
all three paths:

* the path must already exist as a file -- a missing source raises
  :class:`FileNotFoundError` naming it, and nothing is created;
* it is opened through a ``file:`` URI built by :meth:`pathlib.Path.as_uri`
  (so ``?``, ``#``, ``%`` and spaces in a path cannot smuggle URI parameters)
  with ``mode=ro``;
* ``PRAGMA query_only`` is switched on, so even a statement that got past the
  callers' SELECT-only validation cannot write.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_read_only(path: str, timeout: float = 5.0) -> sqlite3.Connection:
    """Open the existing SQLite file at ``path`` read-only, or raise."""
    target = Path(path).expanduser()
    if not target.is_file():
        raise FileNotFoundError(f"sqlite source {path!r} does not exist (never created)")
    uri = f"{target.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=timeout)
    try:
        connection.execute("PRAGMA query_only = ON")
    except BaseException:
        connection.close()
        raise
    return connection
