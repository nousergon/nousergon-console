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
- **Process memory only.** Nothing is persisted; a restart starts cold. The
  console process is long-lived (the supervisor thread rebuilds the index in
  place, ``console/index/build.py``), so one cold pass per deploy is the whole
  residual cost. Same discipline as ``console/s3_body_cache.py``.

Callers get copies, never the cached objects, so no adapter can mutate what
the next pass reads.
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable

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


class ExecutionCache:
    """Terminal executions' describe results and entered-state lists, keyed by
    executionArn, valid only against the latest listing of their machine."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: executionArn -> (stateMachineArn, tag, describe response)
        self._details: dict[str, tuple[str | None, Tag, dict[str, Any]]] = {}
        #: executionArn -> (stateMachineArn, tag, entered state names)
        self._entered: dict[str, tuple[str | None, Tag, tuple[str, ...]]] = {}

    def observe_listing(self, state_machine_arn: str,
                        summaries: list[dict[str, Any]]) -> None:
        """Record one COMPLETE listing of ``state_machine_arn``; forget what
        it omits. Call only after every page was read — a partial listing
        would evict executions that still exist."""
        listed = {s.get("executionArn") for s in summaries if s.get("executionArn")}
        with self._lock:
            for store in (self._details, self._entered):
                for arn in [a for a, (sm, _t, _v) in store.items()
                            if sm == state_machine_arn and a not in listed]:
                    del store[arn]

    def describe(self, summary: dict[str, Any],
                 fetch: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Cached ``describe_execution`` response when the summary's tag
        matches a terminal entry, else ``fetch()`` (exceptions propagate)."""
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
                else:
                    self._details.pop(arn, None)
        return detail

    def entered_states(self, rec: dict[str, Any],
                       fetch: Callable[[], list[str] | None]) -> list[str] | None:
        """Cached entered-state names for a terminal execution, else
        ``fetch()``. A ``None`` fetch (history unreadable) is never cached."""
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
                else:
                    self._entered.pop(arn, None)
        return entered

    def __len__(self) -> int:
        with self._lock:
            return len(self._details) + len(self._entered)

    def clear(self) -> None:
        """Drop everything. For tests."""
        with self._lock:
            self._details.clear()
            self._entered.clear()


#: One per process — shared by the state-machine adapter, its driver twin and
#: pipeline-reliability's history attach.
EXECUTION_CACHE = ExecutionCache()
