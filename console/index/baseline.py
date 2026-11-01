"""Which red rows are new since yesterday, and which cleared (CN-4.3 `.2`).

Brian, 2026-10-29: the console must get materially better every day, and a
day the not-healthy count does not fall must be explained. A count alone
cannot explain itself: 148 -> 151 might be three new failures or ten cleared
and thirteen new. So `/exceptions` compares today's list with the published
snapshot of a previous day (`nous-ergon-ops/scripts/console_exceptions_snapshot.py`
writes `ops/console/exceptions-YYYY-MM-DD.json`) and marks each row `new`,
and names the rows that cleared.

Declared in config as::

    exceptions_baseline:
      bucket: alpha-engine-research
      key: ops/console/exceptions-{date}.json   # {date} = UTC yesterday

Unreadable baseline = a FAILED `exceptions-baseline` source, and no row is
marked new; it never claims every row is new.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..model.envelope import AdapterResult, AdapterStatus

SOURCE_NAME = "exceptions-baseline"
CADENCE_SECONDS = 86_400.0


@dataclass(frozen=True)
class BaselineSource:
    bucket: str
    key: str
    region: str | None = None
    days_back: int = 1

    def resolved_key(self, now: datetime) -> str:
        day = (now.astimezone(timezone.utc) - timedelta(days=self.days_back)).date()
        return self.key.replace("{date}", day.isoformat())


@dataclass(frozen=True)
class Baseline:
    """The previous list: its row ids, labels, and when it was taken."""

    ids: frozenset[str]
    labels: dict[str, str]
    taken_at: str | None


def parse(raw: Any) -> BaselineSource | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw.get("bucket") or not raw.get("key"):
        raise ValueError("exceptions_baseline must be a mapping with `bucket` and `key`")
    days = raw.get("days_back", 1)
    if not isinstance(days, int) or days < 1:
        raise ValueError("exceptions_baseline.days_back must be a whole number of days >= 1")
    return BaselineSource(bucket=str(raw["bucket"]), key=str(raw["key"]),
                          region=raw.get("region"), days_back=days)


def from_snapshot(doc: Any) -> Baseline:
    if not isinstance(doc, dict) or doc.get("view") != "exceptions" \
            or not isinstance(doc.get("rows"), list):
        raise ValueError("baseline is not a console exceptions snapshot")
    ids, labels = set(), {}
    for row in doc["rows"]:
        if isinstance(row, dict) and row.get("id"):
            ids.add(str(row["id"]))
            if row.get("label"):
                labels[str(row["id"])] = str(row["label"])
    return Baseline(ids=frozenset(ids), labels=labels,
                    taken_at=doc.get("snapshot_utc"))


def _s3_read(src: BaselineSource, key: str) -> bytes:
    from .. import aws

    return aws.client("s3", src.region).get_object(
        Bucket=src.bucket, Key=key)["Body"].read()


def read(src: BaselineSource, now: datetime,
         reader: Callable[[BaselineSource, str], bytes] | None = None,
         ) -> tuple[Baseline | None, AdapterResult]:
    """Never raises: a failure is a FAILED source with the reason."""
    key = src.resolved_key(now)
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        base = from_snapshot(json.loads((reader or _s3_read)(src, key)))
    except Exception as exc:  # noqa: BLE001 - rendered as a failed source
        return None, AdapterResult(
            name=SOURCE_NAME, status=AdapterStatus.FAILED, fetched_at=stamp,
            declared_cadence_seconds=CADENCE_SECONDS,
            unavailable=(f"s3://{src.bucket}/{key}: {type(exc).__name__}: {exc}"[:300],))
    return base, AdapterResult(
        name=SOURCE_NAME, status=AdapterStatus.OK,
        fetched_at=base.taken_at or stamp, declared_cadence_seconds=CADENCE_SECONDS)


def compare(base: Baseline | None, current_ids: list[str]) -> dict[str, Any] | None:
    """New and cleared ids against the baseline, or None with no baseline."""
    if base is None:
        return None
    now = set(current_ids)
    return {"taken_at": base.taken_at,
            "new": sorted(now - base.ids),
            "cleared": sorted(base.ids - now),
            "cleared_labels": {i: base.labels[i] for i in base.ids - now if i in base.labels}}
