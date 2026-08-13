"""One boto3 client per (service, region), for the life of the process.

WHY THIS EXISTS
---------------
The index rebuilds on a cadence, forever, and every adapter that talks to AWS
was constructing a fresh client on every call. Sixteen call sites across twelve
modules, none of them cached, and two of the worst are inside inner functions:
``cloudwatch_metrics._client()`` is a factory invoked separately by
``enumerator()`` and ``reader()``, and ``pipeline_reliability.attach()``
constructs a ``stepfunctions`` client each time it is called.

A boto3 client is not a handle. Constructing one resolves the endpoint, loads
and parses the service's JSON model, builds a serializer and a parser, and
registers a full set of event handlers. Paying that on a 60-second loop is a
cost with no benefit: a botocore client is thread-safe for calls, holds no
per-request state worth discarding, and is exactly the object you are meant to
keep.

Measured before this landed (2026-08-12, live box): one full ``build_index``
pass took **93.5 s wall and peaked at 159 MiB RSS** over 315 entities, against
a declared 60 s refresh cadence — so the process spent 61% of every cycle
rebuilding, and its ~163 MiB working set is that builder rather than the served
index. `alpha-engine-config-I7124`.

WHAT THIS DELIBERATELY DOES NOT CHANGE
--------------------------------------
Call sites keep their own ``try: import boto3 / except ImportError`` guard.
That guard is not ceremony — it is how an adapter fails LOUD when the AWS extra
is absent, rather than quietly returning zero rows and painting a substrate
ABSENT on a missing dependency (§5.5). Only the construction moves here; the
detection of a missing boto3 stays where the handling for it lives.
"""

from __future__ import annotations

import threading
from typing import Any

_CLIENTS: dict[tuple[str, str | None], Any] = {}
_LOCK = threading.Lock()


def client(service: str, region: str | None = None) -> Any:
    """A shared boto3 client for ``service`` in ``region``.

    Raises ``ImportError`` when boto3 is absent, exactly as ``boto3.client``
    would, so a caller's existing guard keeps working unchanged.

    Keyed on region as well as service because the same service is legitimately
    reached in more than one region — collapsing that key would silently send a
    caller's request to whichever region asked first, which is worse than the
    cost this function exists to remove.
    """
    key = (service, region)
    existing = _CLIENTS.get(key)
    if existing is not None:
        return existing
    # Imported here, not at module scope: this module is imported by adapters
    # that must remain importable without the AWS extra installed.
    import boto3  # type: ignore[import-not-found]

    with _LOCK:
        # Re-check under the lock: the supervisor thread rebuilds the index
        # while request threads serve, so two callers can miss simultaneously.
        # Losing that race would construct two clients and keep one, which is
        # harmless but is exactly the waste this module removes.
        existing = _CLIENTS.get(key)
        if existing is None:
            existing = (
                boto3.client(service, region_name=region) if region
                else boto3.client(service)
            )
            _CLIENTS[key] = existing
    return existing


def reset() -> None:
    """Drop every cached client. For tests, and for nothing else.

    Deliberately not wired to any runtime path: a client that has gone bad is a
    credential or endpoint problem, and silently rebuilding one on a schedule
    would hide it rather than surface it.
    """
    with _LOCK:
        _CLIENTS.clear()
