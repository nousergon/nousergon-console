"""Which tracked issue owns each red row, and at what priority (CN-4.3 `.2`).

`/exceptions` orders rows P0, untriaged, P1-P3 (`pages.priority_rank`), but a
component or run row carries no priority of its own: the priority belongs to
the issue that tracks the fix. Without this overlay every such row reads
"untriaged" however well the backlog has triaged it — measured 2026-10-30,
164 of 164 rows.

The overlay is a single JSON object the nightly plan writes after it files or
finds the issue for each row::

    {"generated_utc": "2026-10-30T11:00:00Z",
     "rows": {"<row id>": {"issue": "<repo>-I<N>", "url": "https://...",
                           "priority": "P1"}}}

Declared in config as::

    triage:
      bucket: alpha-engine-research
      key: ops/daily-summaries/console-triage.json

It is read once per build and stamps two facets on each matching row:
`priority` (only when the row's own source gave none — an issue row's own
label is the authority on itself) and `tracked_by`, plus `tracked_by_url`.
A row the map does not name is untouched and stays untriaged; a map that
cannot be read is a FAILED source named on every page, never a silent
"everything untriaged".
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from ..model.envelope import AdapterResult, AdapterStatus

#: The source name every page shows for this read.
SOURCE_NAME = "triage-overlay"
#: The map is written by a nightly run; a day and a half without one is stale.
CADENCE_SECONDS = 86_400.0
_PRIORITY = re.compile(r"^P[0-3]$")


@dataclass(frozen=True)
class TriageSource:
    bucket: str
    key: str
    region: str | None = None


@dataclass(frozen=True)
class Mark:
    """One row's tracking issue. `priority` is None when the map gave none."""

    issue: str
    url: str | None
    priority: str | None


def parse(raw: Any) -> TriageSource | None:
    """The `triage:` block, or None when it is not declared."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw.get("bucket") or not raw.get("key"):
        raise ValueError("triage must be a mapping with `bucket` and `key`")
    return TriageSource(bucket=str(raw["bucket"]), key=str(raw["key"]),
                        region=raw.get("region"))


def marks(doc: Any) -> dict[str, Mark]:
    """The row -> issue map. Raises ValueError naming the first bad entry.

    Strict on purpose: a priority the console cannot rank would render as
    untriaged, which is the silent outcome this overlay exists to remove."""
    if not isinstance(doc, dict) or not isinstance(doc.get("rows"), dict):
        raise ValueError("triage map has no `rows` mapping")
    out: dict[str, Mark] = {}
    for row_id, entry in doc["rows"].items():
        if not isinstance(entry, dict) or not str(entry.get("issue") or "").strip():
            raise ValueError(f"triage row {row_id!r} names no issue")
        prio = entry.get("priority")
        if prio is not None:
            prio = str(prio).strip().upper()
            if not _PRIORITY.match(prio):
                raise ValueError(f"triage row {row_id!r} has priority {entry['priority']!r}; "
                                 "expected P0-P3")
        url = entry.get("url")
        out[str(row_id)] = Mark(issue=str(entry["issue"]).strip(),
                                url=str(url) if url else None, priority=prio)
    return out


def _s3_read(src: TriageSource) -> bytes:
    from .. import aws

    return aws.client("s3", src.region).get_object(
        Bucket=src.bucket, Key=src.key)["Body"].read()


def read(src: TriageSource, now: str,
         reader: Callable[[TriageSource], bytes] | None = None,
         ) -> tuple[dict[str, Mark], AdapterResult]:
    """Read the map. Never raises: a failure is a FAILED source with the reason."""
    try:
        doc = json.loads((reader or _s3_read)(src))
        found = marks(doc)
    except Exception as exc:  # noqa: BLE001 - rendered as a failed source
        return {}, AdapterResult(
            name=SOURCE_NAME, status=AdapterStatus.FAILED, fetched_at=now,
            declared_cadence_seconds=CADENCE_SECONDS,
            unavailable=(f"s3://{src.bucket}/{src.key}: {type(exc).__name__}: {exc}"[:300],))
    return found, AdapterResult(
        name=SOURCE_NAME, status=AdapterStatus.OK,
        fetched_at=str(doc.get("generated_utc") or now),
        declared_cadence_seconds=CADENCE_SECONDS)


def stamp(facets: dict[str, str], mark: Mark, *, own_priority: bool = False,
          ) -> dict[str, str]:
    """The row's facets with its tracking issue added. `own_priority` is True
    when the row's source already ranks it (a facet or a `priority` field)."""
    out = dict(facets)
    out["tracked_by"] = mark.issue
    if mark.url:
        out["tracked_by_url"] = mark.url
    if mark.priority and not own_priority and not out.get("priority"):
        out["priority"] = mark.priority
    return out
