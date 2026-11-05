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
      trend_key: ops/console/exceptions-trend.json   # optional

`trend_key` names the small day-by-day count history the same snapshot job
keeps (one point per UTC day: total and per-priority counts). Home and
`/exceptions` chart it, so "is the red going down?" is answered on the page
rather than in a thread. Unreadable trend = a FAILED `exceptions-trend`
source and no chart; it never draws a flat line it did not read.

Unreadable baseline = a FAILED `exceptions-baseline` source, and no row is
marked new; it never claims every row is new.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..model.envelope import AdapterResult, AdapterStatus

SOURCE_NAME = "exceptions-baseline"
TREND_SOURCE_NAME = "exceptions-trend"
CADENCE_SECONDS = 86_400.0
#: The most days a chart draws; older points stay in the file, unshown.
TREND_DAYS = 30


@dataclass(frozen=True)
class BaselineSource:
    bucket: str
    key: str
    region: str | None = None
    days_back: int = 1
    trend_key: str | None = None

    def resolved_key(self, now: datetime) -> str:
        day = (now.astimezone(timezone.utc) - timedelta(days=self.days_back)).date()
        return self.key.replace("{date}", day.isoformat())


@dataclass(frozen=True)
class Baseline:
    """The previous list: its row ids, labels, and when it was taken."""

    ids: frozenset[str]
    labels: dict[str, str]
    taken_at: str | None
    #: id -> row_key(); rows absent here compare by id.
    keys: dict[str, str] = field(default_factory=dict)


def row_key(row_id: str, kind: str | None, state: str | None,
            job: str | None = None) -> str:
    """What a row stands for across days. A run row is folded to one row per
    job and state whose representative id is its NEWEST run, so a job still
    failing under a newer run must not read as cleared-and-new: runs compare
    by job and state; every other row by its id.

    The job is the folding logic's (`render.json.latest_per_job`): the run's
    `belongs-to` target when the caller knows it, else the id before ``@``. A
    systemd run's id is its bare InvocationID, linked to its unit only by
    `belongs-to`, so keying on ``@`` alone read a unit still failing under a
    newer invocation as cleared-and-new (codex 10fe7de16b3c53ab)."""
    if kind == "run":
        job = job or (row_id.split("@", 1)[0] if "@" in row_id else None)
        if job:
            return f"run|{job}|{state}"
    return row_id


def parse(raw: Any) -> BaselineSource | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw.get("bucket") or not raw.get("key"):
        raise ValueError("exceptions_baseline must be a mapping with `bucket` and `key`")
    days = raw.get("days_back", 1)
    if not isinstance(days, int) or days < 1:
        raise ValueError("exceptions_baseline.days_back must be a whole number of days >= 1")
    trend = raw.get("trend_key")
    if trend is not None and (not isinstance(trend, str) or not trend.strip()):
        raise ValueError("exceptions_baseline.trend_key must be a non-empty S3 key")
    return BaselineSource(bucket=str(raw["bucket"]), key=str(raw["key"]),
                          region=raw.get("region"), days_back=days,
                          trend_key=trend.strip() if trend else None)


def from_snapshot(doc: Any) -> Baseline:
    if not isinstance(doc, dict) or doc.get("view") != "exceptions" \
            or not isinstance(doc.get("rows"), list):
        raise ValueError("baseline is not a console exceptions snapshot")
    ids, labels, keys = set(), {}, {}
    for row in doc["rows"]:
        if isinstance(row, dict) and row.get("id"):
            rid = str(row["id"])
            ids.add(rid)
            job = row.get("job")
            keys[rid] = row_key(rid, row.get("kind"), row.get("state"),
                                str(job) if job else None)
            if row.get("label"):
                labels[rid] = str(row["label"])
    return Baseline(ids=frozenset(ids), labels=labels,
                    taken_at=doc.get("snapshot_utc"), keys=keys)


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


def compare(base: Baseline | None, current_ids: list[str],
            current_keys: dict[str, str] | None = None) -> dict[str, Any] | None:
    """New and cleared ids against the baseline, or None with no baseline.

    Rows match on ``row_key`` (``current_keys``: id -> key for today's rows),
    so a folded job whose newest run changed is neither new nor cleared."""
    if base is None:
        return None
    cur = {i: (current_keys or {}).get(i, i) for i in current_ids}
    old = {i: base.keys.get(i, i) for i in base.ids}
    old_keys, cur_keys = set(old.values()), set(cur.values())
    cleared = {i for i, k in old.items() if k not in cur_keys}
    return {"taken_at": base.taken_at,
            "new": sorted(i for i, k in cur.items() if k not in old_keys),
            "cleared": sorted(cleared),
            "cleared_labels": {i: base.labels[i] for i in cleared if i in base.labels}}


@dataclass(frozen=True)
class TrendPoint:
    date: str
    total: int
    by_priority: dict[str, int]


def trend_points(doc: Any) -> list[TrendPoint]:
    """The day-by-day counts, oldest first, at most `TREND_DAYS`. Raises
    ValueError on a malformed file: a guessed count is worse than no chart."""
    if not isinstance(doc, dict) or not isinstance(doc.get("days"), list):
        raise ValueError("trend file has no `days` list")
    by_date: dict[str, TrendPoint] = {}
    for d in doc["days"]:
        if not isinstance(d, dict) or not isinstance(d.get("date"), str):
            raise ValueError(f"trend entry {d!r} names no date")
        total = d.get("total")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise ValueError(f"trend entry {d['date']} has total {total!r}")
        prio = d.get("by_priority") or {}
        if not isinstance(prio, dict):
            raise ValueError(f"trend entry {d['date']} has a non-mapping by_priority")
        by_date[d["date"]] = TrendPoint(
            date=d["date"], total=total,
            by_priority={str(k): int(v) for k, v in prio.items()})
    return [by_date[k] for k in sorted(by_date)][-TREND_DAYS:]


def read_trend(src: BaselineSource, now: datetime,
               reader: Callable[[BaselineSource, str], bytes] | None = None,
               ) -> tuple[list[TrendPoint] | None, AdapterResult | None]:
    """The trend, or (None, None) when no `trend_key` is declared. Never
    raises: a failure is a FAILED `exceptions-trend` source."""
    if not src.trend_key:
        return None, None
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        doc = json.loads((reader or _s3_read)(src, src.trend_key))
        points = trend_points(doc)
    except Exception as exc:  # noqa: BLE001 - rendered as a failed source
        return None, AdapterResult(
            name=TREND_SOURCE_NAME, status=AdapterStatus.FAILED, fetched_at=stamp,
            declared_cadence_seconds=CADENCE_SECONDS,
            unavailable=(f"s3://{src.bucket}/{src.trend_key}: "
                         f"{type(exc).__name__}: {exc}"[:300],))
    return points, AdapterResult(
        name=TREND_SOURCE_NAME, status=AdapterStatus.OK,
        fetched_at=str(doc.get("updated_utc") or stamp),
        declared_cadence_seconds=CADENCE_SECONDS)
