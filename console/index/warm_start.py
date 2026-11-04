"""Serve the last good index across a restart, marked, while the first build runs.

WHY THIS EXISTS
---------------
The console restarts on every config apply and every code deploy, several
times a day, and a full build now takes ~350 seconds on the live box (measured
2026-11-04 18:02-18:08Z; 93.5s in August). Until that first build lands the
surface served an empty index, so for six minutes after each restart home
showed nothing at all. Brian, 2026-11-04: "the entire thing is broken."

So after every successful build the index is written to
``$CACHE_DIRECTORY/last_index.pickle`` (systemd ``CacheDirectory=``), and a
restart seeds the surface from it instead of from an empty index.

THE RULES THAT KEEP IT HONEST
-----------------------------
- **A loaded index is never served as current.** It carries ``bootstrap=True``,
  ``stale_since`` = the restart, ``warm_start_from`` = when it was built, and a
  ``last_error`` that still says "first index build has not completed yet", so
  the page shows SURFACE STALE and every consumer that waits on the bootstrap
  window (the deploy probes) still waits for the real build.
- **Nothing is owned.** The file is a cache of what the console last rendered,
  never a source; deleting it means one cold start, exactly as before.
- **A file this code cannot render is ignored.** The payload carries a format
  version and a digest of the console's source; on a digest mismatch (a code
  deploy) the loaded index must render home, the site index and the JSON dump
  without raising, or it is discarded and the start is cold.
- The file is written atomically (temp file + rename), mode 0600. It is only
  ever read back by the process that wrote it, from a directory only that
  service user can write.
"""
from __future__ import annotations

import hashlib
import logging
import os
import pickle
import tempfile
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

#: Bumped whenever the payload shape changes; any other value is ignored.
FORMAT_VERSION = 1

#: Explicit path; wins over ``$CACHE_DIRECTORY``.
PATH_ENV = "NOUSERGON_CONSOLE_INDEX_CACHE"
FILENAME = "last_index.pickle"

_PACKAGE = Path(__file__).resolve().parent.parent


def default_path() -> str | None:
    """Where the last index is kept, or ``None`` (tests, a laptop)."""
    explicit = os.environ.get(PATH_ENV)
    if explicit:
        return explicit
    cache_dir = (os.environ.get("CACHE_DIRECTORY") or "").split(":")[0]
    return os.path.join(cache_dir, FILENAME) if cache_dir else None


def code_digest() -> str:
    """A digest of every module in the console package."""
    h = hashlib.sha256()
    for p in sorted(_PACKAGE.rglob("*.py")):
        h.update(str(p.relative_to(_PACKAGE)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def save(index: Any, path: str | None, digest: str) -> bool:
    """Write ``index`` for the next start. Never raises."""
    if not path:
        return False
    tmp = None
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".last_index.", dir=os.path.dirname(path) or ".")
        with os.fdopen(fd, "wb") as fh:
            pickle.dump({"version": FORMAT_VERSION, "digest": digest, "index": index},
                        fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except Exception as exc:  # noqa: BLE001 - a cache write never breaks a build
        _LOG.warning("warm start: could not write %s: %s", path, exc)
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


def _renders(index: Any) -> bool:
    from ..render import html, json as render_json

    try:
        html.landing_page(index)
        html.dashboards_page(index)
        render_json.dumps(render_json.index_freshness(index))
        return True
    except Exception as exc:  # noqa: BLE001 - any failure means cold start
        _LOG.warning("warm start: cached index does not render under this code: %s", exc)
        return False


def load(path: str | None, digest: str) -> Any | None:
    """The last saved index, or ``None`` when there is none this code can serve."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as fh:
            payload = pickle.load(fh)  # noqa: S301 - written by this service only
    except Exception as exc:  # noqa: BLE001 - corrupt or foreign: cold start
        _LOG.warning("warm start: ignoring unreadable %s: %s", path, exc)
        return None
    if not isinstance(payload, dict) or payload.get("version") != FORMAT_VERSION:
        return None
    index = payload.get("index")
    if index is None or getattr(index, "build_info", None) is None:
        return None
    if payload.get("digest") != digest and not _renders(index):
        return None
    return index
