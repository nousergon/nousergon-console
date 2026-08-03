"""checks-envelope adapter — Component state from fleet check-result envelopes.

Reads an object-store prefix of ``…/<check_id>/latest.json`` envelopes and maps
each body onto a Component entity carrying the §5.1 four-field row contract
(state · source · as-of · evidence). The envelope shape is the one every
scheduled fleet check publishes:

    { "schema_version": 1,
      "check_id":  "<id>",
      "label":     "…",
      "ran_at":    "ISO-8601",
      "status":    "ok" | "attention" | "error",
      "summary":   "…",
      "cadence_minutes": <int>,
      "deep_link": "…",          # optional
      "findings":  […] }

Why a dedicated adapter rather than ``object-store`` plus config: the generic
object-store adapter projects **keys → Artifact entities** and derives
staleness from last-modified alone. The checks envelope carries its own
``status``, ``ran_at`` and ``cadence_minutes`` inside the body, and the entity
kind that must render is **Component** (the check *is* the component's
reporting row), not Artifact. Parsing the body is a different projection, so
it is a different adapter (§2.3 — one source shape per adapter).

``check_id`` is the source-assigned identifier and is used verbatim as the
component id (§3.6 one-namespace). The adapter never mints slugs.

Hermetic: listing and body-reading are two injectable callables so tests run
over recorded fixtures with no live bucket (groom-sweep §8.1).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from ..model.entity import Edge, Entity, Provenance
from ..model.envelope import AdapterResult, AdapterStatus
from ..model.kinds import Kind, State

name = "checks"
produces = ("component", "run", "artifact")

#: (key, last_modified_iso_or_None)
StoredObject = tuple[str, str | None]
StoreLister = Callable[[str, str], list[StoredObject]]
#: A body reader takes (bucket, key) and returns the decoded envelope dict, or
#: raises when the object is unreadable.
BodyReader = Callable[[str, str], dict[str, Any]]

_DEFAULT_KEY_PATTERN = r".*/(?P<component_id>[^/]+)/latest\.json$"


def fetch(
    config: dict[str, Any],
    lister: StoreLister | None = None,
    reader: BodyReader | None = None,
    now: datetime | None = None,
) -> AdapterResult:
    bucket = config.get("bucket")
    prefix = config.get("prefix", "")
    pattern = config.get("key_pattern") or _DEFAULT_KEY_PATTERN
    if not bucket:
        return AdapterResult(
            name=config.get("_name", name),
            status=AdapterStatus.FAILED,
            unavailable=("all",),
        )
    if lister is None or reader is None:
        default_lister, default_reader = _default_s3()
        lister = lister or default_lister
        reader = reader or default_reader
        missing = []
        if lister is None:
            missing.append("lister")
        if reader is None:
            missing.append("reader")
        if missing:
            return AdapterResult(
                name=config.get("_name", name),
                status=AdapterStatus.FAILED,
                unavailable=tuple(missing),
            )

    regex = re.compile(pattern)
    staleness_factor = float(config.get("staleness_factor", 1.5))
    now = now or datetime.now(timezone.utc)
    source_label = f"s3://{bucket}/{prefix}"

    try:
        objects = lister(bucket, prefix)
    except Exception:
        return AdapterResult(
            name=config.get("_name", name),
            status=AdapterStatus.FAILED,
            unavailable=("source",),
        )

    entities: list[Entity] = []
    edges: list[Edge] = []
    partial = False

    for key, last_modified in objects:
        # search (not match) so a pattern written against the tail of the key
        # still hits when the lister returns full keys under a broader prefix.
        m = regex.search(key)
        if not m:
            continue
        groups = m.groupdict()
        try:
            body = reader(bucket, key)
        except Exception:
            # An unreadable envelope is a declared absence for that check — the
            # component still appears, UNREPORTED, so the gap is visible (§2.3).
            cid = groups.get("component_id") or key
            entities.append(Entity(
                kind=Kind.COMPONENT,
                id=str(cid),
                state=State.UNREPORTED,
                provenance=Provenance(
                    source=source_label,
                    as_of=last_modified,
                    evidence=f"s3://{bucket}/{key}",
                ),
                detail={"key": key, "unreadable": True},
            ))
            partial = True
            continue

        mapped = _from_envelope(body, key, bucket, source_label, last_modified,
                                staleness_factor, now, groups)
        if mapped is None:
            partial = True
            continue
        component, run, art, run_edge, produces_edge = mapped
        entities.append(component)
        entities.append(art)
        if run is not None:
            entities.append(run)
            edges.append(run_edge)  # type: ignore[arg-type]
        edges.append(produces_edge)

    return AdapterResult(
        name=config.get("_name", name),
        status=AdapterStatus.OK,
        entities=tuple(entities),
        edges=tuple(edges),
        unavailable=("body",) if partial else (),
    )


def _from_envelope(
    body: dict[str, Any],
    key: str,
    bucket: str,
    source_label: str,
    last_modified: str | None,
    staleness_factor: float,
    now: datetime,
    groups: dict[str, str | None],
) -> tuple[Entity, Entity | None, Entity, Edge | None, Edge] | None:
    """Project one envelope onto Component + optional Run + Artifact + edges."""
    # check_id is authoritative when present; the path segment is the fallback.
    # Both must agree with the one-namespace rule — we never mint a third id.
    check_id = str(body.get("check_id") or groups.get("component_id") or "")
    if not check_id:
        return None

    ran_at = body.get("ran_at") or last_modified
    cadence_minutes = body.get("cadence_minutes")
    status = str(body.get("status") or "").lower()
    deep_link = body.get("deep_link")
    evidence = str(deep_link) if deep_link else f"s3://{bucket}/{key}"

    state = _component_state(status, ran_at, cadence_minutes, staleness_factor, now)

    component = Entity(
        kind=Kind.COMPONENT,
        id=check_id,  # verbatim — never slug-minted (§3.6)
        state=state,
        provenance=Provenance(
            source=source_label,
            as_of=str(ran_at) if ran_at else None,
            evidence=evidence,
        ),
        detail={
            "label": body.get("label"),
            "summary": body.get("summary"),
            "status": status or None,
            "cadence_minutes": cadence_minutes,
            "findings_count": len(body.get("findings") or []),
            "schema_version": body.get("schema_version"),
            "key": key,
        },
    )

    art = Entity(
        kind=Kind.ARTIFACT,
        id=key,  # the key is the source-assigned artifact id (§2.1)
        state=state,
        provenance=Provenance(
            source=source_label,
            as_of=str(ran_at) if ran_at else None,
            evidence=f"s3://{bucket}/{key}",
        ),
        detail={"check_id": check_id, "schema_version": body.get("schema_version")},
    )
    produces_edge = Edge(source=check_id, rel="produces", target=key)

    # One Run per envelope publish — the check's most recent execution. The run
    # id is the artifact key plus ran_at so successive publishes are distinct
    # when both are present; falling back to the key alone is still source-assigned.
    run = None
    run_edge = None
    if ran_at:
        run_id = f"{check_id}@{ran_at}"
        run = Entity(
            kind=Kind.RUN,
            id=run_id,
            state=_status_to_state(status),
            provenance=Provenance(
                source=source_label,
                as_of=str(ran_at),
                evidence=evidence,
            ),
            detail={"check_id": check_id, "summary": body.get("summary")},
        )
        run_edge = Edge(source=run_id, rel="belongs-to", target=check_id)

    return component, run, art, run_edge, produces_edge


def _component_state(
    status: str,
    ran_at: Any,
    cadence_minutes: Any,
    staleness_factor: float,
    now: datetime,
) -> State:
    """Status first, then staleness. A dying check's last write is almost
    always ``ok`` — ``ran_at`` + ``cadence_minutes`` are what catch it when it
    stops publishing, whatever status it last wrote.

    Past its cadence the component is §8.3's **MISSED**: the schedule fired or
    should have and no run started, which is a failure UPSTREAM of the
    component, in its trigger. Not STALLED (nothing reported a start) and not
    FAILED (it did not stop, it never began)."""
    base = _status_to_state(status)
    if ran_at is None or cadence_minutes in (None, "", 0):
        # No freshness inputs → cannot compute staleness; return the status
        # mapping (UNKNOWN if status itself was missing).
        return base
    try:
        ts = datetime.fromisoformat(str(ran_at).replace("Z", "+00:00"))
        cadence_s = float(cadence_minutes) * 60.0
    except (TypeError, ValueError):
        return base
    age = (now - ts).total_seconds()
    if age > cadence_s * staleness_factor:
        return State.MISSED
    return base


def _status_to_state(status: str) -> State:
    if status == "ok":
        return State.HEALTHY
    if status == "attention":
        return State.DEGRADED
    if status == "error":
        return State.FAILED
    # A missing status, or one outside the envelope's own vocabulary, is a
    # component this classifier cannot place. §8.3's answer is UNREPORTED —
    # loud, and a finding — never a fall-through and never green.
    return State.UNREPORTED


def _default_s3() -> tuple[StoreLister | None, BodyReader | None]:
    """boto3-backed lister + body reader when the optional AWS extra is installed.

    Returns ``(None, None)`` when boto3 is absent so the adapter fails loud
    rather than silently returning zero rows (§5.5).
    """
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ImportError:
        return None, None

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

    def reader(bucket: str, key: str) -> dict[str, Any]:
        client = boto3.client("s3")
        try:
            resp = client.get_object(Bucket=bucket, Key=key)
            body = resp["Body"].read()
        except (BotoCoreError, ClientError):
            raise
        return json.loads(body.decode("utf-8"))

    return lister, reader
