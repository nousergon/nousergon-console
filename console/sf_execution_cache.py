"""Read a FINISHED Step Functions execution once, not on every refresh pass.

WHY THIS EXISTS
---------------
The index rebuilds every ``refresh_seconds`` (180 s on the fleet), and every
rebuild re-read every execution Step Functions still retains: one
``DescribeExecution`` per execution ``list_executions`` returned, plus paged
``GetExecutionHistory`` for the executions the pipeline-reliability classifier
reads. Measured 2026-10-07 (CloudTrail, the console's own role): ~250 SF read
calls a minute around the clock — ~110/min ``DescribeExecution`` and
~120-160/min ``GetExecutionHistory`` — against a handful of state machines
running a few times a day. Those responses leave the region over the public
internet when the host has no Step Functions VPC endpoint, ~9 GB a day, and
are billed as ``DataTransfer-Out-Bytes`` once the free egress tier runs out
(alpha-engine-config cost finding, 2026-10-07).

A terminal execution (SUCCEEDED / FAILED / TIMED_OUT / ABORTED, with a
``stopDate``) does not change, so the previous pass's answer IS the current
answer and the re-read buys nothing.

THE RULES THAT KEEP IT HONEST
-----------------------------
- **Only the LISTING can make a cached answer reusable.** Each cached answer is
  filed under the execution's change tag — status, ``stopDate`` and
  ``redriveCount`` — and served only when the most recent ``list_executions``
  summary carries the same tag AND that tag is terminal. A RUNNING execution is
  never cached, so it is re-read every pass; a FAILED execution that is later
  REDRIVEN (same ARN, status back to RUNNING, ``redriveCount`` + 1) stops
  matching and is re-read.
- **The tag comes from the response the bytes came with**, not from the
  listing, so an execution that finished between the LIST and the DESCRIBE is
  filed under its new (terminal) tag and the next listing matches it — never
  the reverse.
- **An execution gone from the listing is gone from the cache.**
  ``observe_listing`` drops every cached entry for that state machine the
  complete listing no longer names, so the cache is bounded by exactly the set
  the adapters can see: Step Functions' own retention window, which is also the
  pipeline-reliability floor (I7068). Nothing ages out silently that the
  adapter would still render, and nothing outlives it.
- **Persisted across restarts when the host gives it a directory**
  (alpha-engine-config-I11792). The rules above were process-memory only, on
  the reasoning that one cold pass per deploy was the whole residual cost.
  Measured 2026-10-14 (CloudTrail, ``alpha-engine-dashboard-role``): that pass
  is ~400 ``GetExecutionHistory`` calls — ~330 of them paging the weekly
  pipeline's 1000-event history pages (one rerun alone is 44 pages) — and the
  console restarted seven times that afternoon (two code deploys, five
  ``config.d`` applies, each a ``systemctl restart``), so restarts, not
  refreshes, were ~800 calls an hour against ~5-13 on a quiet hour. A
  terminal execution's answer does not change when the process does, so the
  cache is written to ``$CACHE_DIRECTORY/sf_execution_cache.json`` (systemd
  ``CacheDirectory=``; ``NOUSERGON_CONSOLE_SF_CACHE`` names a file explicitly)
  and read back on first use. Persisting changes nothing about WHEN an entry
  is served: a loaded entry is still served only against a matching terminal
  listing tag, and the first complete listing still evicts what it omits. The
  file is written atomically (temp file + rename), and an unreadable, foreign
  or corrupt file is ignored — the cache starts cold, exactly as before. With
  neither variable set (tests, a laptop) it stays process-memory only. Same
  discipline as ``console/s3_body_cache.py`` otherwise.

Callers get copies, never the cached objects, so no adapter can mutate what
the next pass reads.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime
from typing import Any, Callable

_LOG = logging.getLogger(__name__)

#: Bumped whenever the on-disk shape changes; a file carrying any other value
#: is ignored (cold start), never half-read.
STORE_VERSION = 1

#: Explicit store path; wins over ``$CACHE_DIRECTORY``.
STORE_PATH_ENV = "NOUSERGON_CONSOLE_SF_CACHE"
STORE_FILENAME = "sf_execution_cache.json"

#: While a cold pass is filling the cache, write at most this often, so a
#: process killed mid-pass (OOM, a restart landing mid-build) keeps what it had
#: already paid for. Every reader also persists once at the end of its read.
_PERSIST_MIN_INTERVAL_S = 15.0

#: Statuses after which Step Functions never changes an execution again —
#: except via RedriveExecution, which the change tag's ``redriveCount`` and
#: status catch. ``PENDING_REDRIVE`` is deliberately absent.
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"})

Tag = tuple[str, str, str]


def _stamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def change_tag(rec: dict[str, Any]) -> Tag | None:
    """The change token for one execution, read off a listing summary, a
    ``describe_execution`` response, or a record merged from both.

    ``None`` unless the execution is terminal AND carries a ``stopDate`` —
    ``None`` means "never reuse, always fetch".
    """
    status = rec.get("status")
    if status not in TERMINAL_STATUSES:
        return None
    stop = _stamp(rec.get("stopDate"))
    if stop is None:
        return None
    return (str(status), stop, str(rec.get("redriveCount") or 0))


def default_store_path() -> str | None:
    """Where this process persists the cache, or ``None`` for memory only.

    ``NOUSERGON_CONSOLE_SF_CACHE`` (a file) wins; else systemd's
    ``CacheDirectory=`` export ``$CACHE_DIRECTORY`` (first entry when several
    are listed, colon-separated).
    """
    explicit = os.environ.get(STORE_PATH_ENV)
    if explicit:
        return explicit
    cache_dir = (os.environ.get("CACHE_DIRECTORY") or "").split(":")[0]
    if cache_dir:
        return os.path.join(cache_dir, STORE_FILENAME)
    return None


def _encode(value: Any) -> Any:
    """JSON-safe copy of a boto3 response: datetimes become ``{"$dt": iso}``
    so they come back as datetimes (``_as_of`` and the change tag read them)."""
    if isinstance(value, datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value


def _decode_hook(obj: dict[str, Any]) -> Any:
    if len(obj) == 1 and "$dt" in obj and isinstance(obj["$dt"], str):
        return datetime.fromisoformat(obj["$dt"])
    return obj


class ExecutionCache:
    """Terminal executions' describe results and entered-state lists, keyed by
    executionArn, valid only against the latest listing of their machine."""

    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.Lock()
        #: executionArn -> (stateMachineArn, tag, describe response)
        self._details: dict[str, tuple[str | None, Tag, dict[str, Any]]] = {}
        #: executionArn -> (stateMachineArn, tag, entered state names)
        self._entered: dict[str, tuple[str | None, Tag, tuple[str, ...]]] = {}
        self._path = path
        self._loaded = path is None
        self._dirty = False
        self._last_persist = 0.0

    # ── persistence (alpha-engine-config-I11792) ──────────────────────────

    def use_store(self, path: str | None) -> None:
        """Point the cache at ``path`` (``None``: memory only). The file is
        read on next use. For process wiring and tests."""
        with self._lock:
            self._path = path
            self._loaded = path is None
            self._dirty = False

    def _load_locked(self) -> None:
        """Read the store once. Caller holds the lock. Anything unexpected
        means a cold start, never a partial or wrong cache."""
        if self._loaded:
            return
        self._loaded = True
        path = self._path
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh, object_hook=_decode_hook)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            _LOG.warning("sf execution cache: ignoring unreadable store %s: %s", path, exc)
            return
        try:
            if not isinstance(doc, dict) or doc.get("version") != STORE_VERSION:
                raise ValueError(f"store version {doc.get('version') if isinstance(doc, dict) else None!r}")
            details = {
                arn: (sm, tuple(tag), dict(value))
                for arn, (sm, tag, value) in doc["details"].items()
                if len(tag) == 3 and isinstance(value, dict)
            }
            entered = {
                arn: (sm, tuple(tag), tuple(str(n) for n in names))
                for arn, (sm, tag, names) in doc["entered"].items()
                if len(tag) == 3
            }
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            _LOG.warning("sf execution cache: ignoring malformed store %s: %s", path, exc)
            return
        # In-process entries (if any arrived first) are newer; they win.
        for arn, entry in details.items():
            self._details.setdefault(arn, entry)
        for arn, entry in entered.items():
            self._entered.setdefault(arn, entry)

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            with self._lock:
                self._load_locked()

    def persist(self, *, force: bool = False) -> bool:
        """Write the cache to its store if anything changed since the last
        write. Never raises: the store is a transport saving, and a pass must
        not fail because a disk write did. Returns whether a write happened."""
        with self._lock:
            path = self._path
            if not path or (not self._dirty and not force):
                return False
            doc = {
                "version": STORE_VERSION,
                "details": {a: [sm, list(t), _encode(v)]
                            for a, (sm, t, v) in self._details.items()},
                "entered": {a: [sm, list(t), list(n)]
                            for a, (sm, t, n) in self._entered.items()},
            }
            self._dirty = False
            self._last_persist = time.monotonic()
        tmp = None
        try:
            directory = os.path.dirname(path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".sf_execution_cache.", dir=directory)
            # The console's one JSON encoder (tests forbid a second); datetimes
            # were already tagged by ``_encode`` so they round-trip as datetimes.
            from .render.json import dumps as _wire_dumps

            body = _wire_dumps(doc)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.replace(tmp, path)
            return True
        except (OSError, TypeError, ValueError) as exc:
            _LOG.warning("sf execution cache: could not persist to %s: %s", path, exc)
            with self._lock:
                self._dirty = True
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False

    def _maybe_persist(self) -> None:
        if self._path and self._dirty and (
            time.monotonic() - self._last_persist >= _PERSIST_MIN_INTERVAL_S
        ):
            self.persist()

    def observe_listing(self, state_machine_arn: str,
                        summaries: list[dict[str, Any]]) -> None:
        """Record one COMPLETE listing of ``state_machine_arn``; forget what
        it omits. Call only after every page was read — a partial listing
        would evict executions that still exist."""
        listed = {s.get("executionArn") for s in summaries if s.get("executionArn")}
        with self._lock:
            self._load_locked()
            for store in (self._details, self._entered):
                for arn in [a for a, (sm, _t, _v) in store.items()
                            if sm == state_machine_arn and a not in listed]:
                    del store[arn]
                    self._dirty = True

    def describe(self, summary: dict[str, Any],
                 fetch: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Cached ``describe_execution`` response when the summary's tag
        matches a terminal entry, else ``fetch()`` (exceptions propagate)."""
        self._ensure_loaded()
        arn = summary.get("executionArn")
        tag = change_tag(summary)
        if arn and tag is not None:
            with self._lock:
                hit = self._details.get(arn)
            if hit is not None and hit[1] == tag:
                return dict(hit[2])
        detail = fetch()
        stored_tag = change_tag(detail)
        if arn:
            with self._lock:
                if stored_tag is not None:
                    sm = detail.get("stateMachineArn") or summary.get("stateMachineArn")
                    self._details[arn] = (sm, stored_tag, dict(detail))
                    self._dirty = True
                elif self._details.pop(arn, None) is not None:
                    self._dirty = True
            self._maybe_persist()
        return detail

    def entered_states(self, rec: dict[str, Any],
                       fetch: Callable[[], list[str] | None]) -> list[str] | None:
        """Cached entered-state names for a terminal execution, else
        ``fetch()``. A ``None`` fetch (history unreadable) is never cached."""
        self._ensure_loaded()
        arn = rec.get("executionArn")
        tag = change_tag(rec)
        if arn and tag is not None:
            with self._lock:
                hit = self._entered.get(arn)
            if hit is not None and hit[1] == tag:
                return list(hit[2])
        entered = fetch()
        if arn:
            with self._lock:
                if tag is not None and entered is not None:
                    self._entered[arn] = (rec.get("stateMachineArn"), tag, tuple(entered))
                    self._dirty = True
                elif self._entered.pop(arn, None) is not None:
                    self._dirty = True
            self._maybe_persist()
        return entered

    def __len__(self) -> int:
        self._ensure_loaded()
        with self._lock:
            return len(self._details) + len(self._entered)

    def clear(self) -> None:
        """Drop everything in memory (the store file is left alone). For tests."""
        with self._lock:
            self._details.clear()
            self._entered.clear()
            self._dirty = False


#: One per process — shared by the state-machine adapter, its driver twin and
#: pipeline-reliability's history attach.
#: Persisted to ``default_store_path()`` when the host provides one.
EXECUTION_CACHE = ExecutionCache(default_store_path())
