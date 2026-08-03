"""object-store adapter — Artifact / Component / Run entities from an S3-compatible prefix.

Lists a configured prefix and maps each key to an entity via a configured
``key_pattern`` regex whose named groups become entity fields
(`config.example.yaml`'s `object-store` block). This is how the fleet's
`ops/checks/<id>/latest.json` envelope and other durable keys become indexed
artifacts and component-state rows.

Generic over "an S3-compatible bucket/prefix with a key pattern" — bucket,
prefix, pattern and cadence all come from configuration (§2.3). The store
client is one injectable function so tests run over recorded key lists with
no live bucket (groom-sweep §8.1).

Staleness is computable when the source supplies a last-modified stamp and the
config declares a ``cadence``: a key older than its cadence ×
``staleness_factor`` renders STALE (§5.2), never as its last value in normal
styling.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable

from ..model.entity import Entity, Provenance
from ..model.envelope import AdapterResult, AdapterStatus
from ..model.kinds import Kind

name = "checks"
produces = ("component", "run", "artifact")

#: One stored object: its key and an optional ISO last-modified stamp.
StoredObject = tuple[str, str | None]
#: A lister takes (bucket, prefix) and returns the objects under it. Injectable.
StoreLister = Callable[[str, str], list[StoredObject]]


def fetch(
    config: dict[str, Any],
    lister: StoreLister | None = None,
    now: datetime | None = None,
) -> AdapterResult:
    bucket = config.get("bucket")
    prefix = config.get("prefix", "")
    pattern = config.get("key_pattern")
    if not bucket or not pattern:
        return AdapterResult(
            name=config.get("_name", name),
            status=AdapterStatus.FAILED,
            unavailable=("all",),
        )
    if lister is None:
        lister = _default_lister()
        if lister is None:
            # boto3 not installed — declare unable rather than silently zero (§5.5).
            return AdapterResult(
                name=config.get("_name", name),
                status=AdapterStatus.FAILED,
                unavailable=("lister",),
            )

    regex = re.compile(pattern)
    cadence = _parse_cadence(config.get("cadence"))
    staleness_factor = float(config.get("staleness_factor", 1.5))
    now = now or datetime.now(timezone.utc)

    try:
        objects = lister(bucket, prefix)
    except Exception:
        return AdapterResult(
            name=config.get("_name", name),
            status=AdapterStatus.FAILED,
            unavailable=("source",),
        )

    entities: list[Entity] = []
    edges = []
    for key, last_modified in objects:
        m = regex.match(key)
        if not m:
            continue
        groups = m.groupdict()
        state = _state(last_modified, cadence, staleness_factor, now)
        art = Entity(
            kind=Kind.ARTIFACT,
            id=key,  # the key is the source-assigned identifier (§2.1)
            state=state,
            provenance=Provenance(
                source=f"s3://{bucket}/{prefix}",
                as_of=last_modified,
                evidence=f"s3://{bucket}/{key}",
            ),
            facets={"repo": bucket} if bucket else {},
            detail={k: v for k, v in groups.items()},
        )
        entities.append(art)
        # If the pattern names a component_id, derive the produces-edge from
        # the registry-side id so the artifact joins to its component (§3.3).
        cid = groups.get("component_id")
        if cid:
            from ..model.entity import Edge

            edges.append(Edge(source=cid, rel="produces", target=key))

    return AdapterResult(
        name=config.get("_name", name),
        status=AdapterStatus.OK,
        entities=tuple(entities),
        edges=tuple(edges),
    )


def _state(
    last_modified: str | None,
    cadence_seconds: float | None,
    staleness_factor: float,
    now: datetime,
) -> str:
    """Staleness is rendered, never inferred by the reader (§5.2).

    This adapter emits **Artifacts**, which do not resolve to component states,
    so §5.1's second half applies and the row carries the value itself. That is
    what keeps the twelve honest: forcing "this object has no cadence declared"
    into a component vocabulary is precisely the pressure that produced the
    `UNKNOWN` fall-through observability-policy.md §8.3 forbids by name.

    The three not-computable cases stay THREE facts, per §5.5 — no stamp, no
    declared cadence and an unparseable stamp are different findings with
    different fixes, and collapsing them loses the fix."""
    if last_modified is None:
        return "no-freshness-stamp"
    if cadence_seconds is None:
        return "no-cadence-declared"
    try:
        ts = datetime.fromisoformat(last_modified.replace("Z", "+00:00"))
    except ValueError:
        return "unreadable"
    age = (now - ts).total_seconds()
    return "fresh" if age <= cadence_seconds * staleness_factor else "stale"


def _default_lister() -> StoreLister | None:
    """boto3-backed lister when the optional AWS extra is installed."""
    try:
        import boto3  # type: ignore
    except ImportError:
        return None

    def lister(bucket: str, prefix: str) -> list[StoredObject]:
        client = boto3.client("s3")
        out: list[StoredObject] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)
            for obj in page.get("Contents") or []:
                key = obj.get("Key") or ""
                lm = obj.get("LastModified")
                if hasattr(lm, "isoformat"):
                    stamp = lm.isoformat()
                else:
                    stamp = str(lm) if lm else None
                out.append((key, stamp))
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        return out

    return lister


def _parse_cadence(cadence: Any) -> float | None:
    """'1h' → 3600, '30m' → 1800, '300' → 300. None when undeclared."""
    if cadence is None:
        return None
    if isinstance(cadence, (int, float)):
        return float(cadence)
    s = str(cadence).strip()
    unit = s[-1]
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit)
    if mult is None:
        return None
    try:
        return float(s[:-1]) * mult
    except ValueError:
        return None
