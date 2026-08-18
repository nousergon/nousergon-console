"""changelog-events adapter — Incident entities from a per-event S3 log.

Reads an object-store prefix of one JSON object per event (the fleet's
changelog event-lake shape: ``changelog/entries/{day}/{event_id}.json`` and,
identically shaped, ``changelog/quarantine/{day}/{event_id}.json`` — schema
1.0.0) and projects each object onto an Incident entity
(`console-policy.md` §2.1, ``nousergon-console#60``).

Why a dedicated adapter rather than ``object-store`` plus config: the generic
object-store adapter always projects **keys → Artifact entities**. This
source's entity kind is Incident, and its state comes from the event body
(a declared field, e.g. ``severity``, or a literal the deployment names for a
whole prefix — e.g. every object under ``changelog/quarantine/`` is a
rejected entry regardless of its own severity), never from key
staleness (§2.3 — one source shape per adapter).

Two configured instances of this same adapter cover both changelog prefixes
(`config.example.yaml`'s `changelog-events` blocks): one over
``changelog/entries/`` with ``state_field: severity``, one over
``changelog/quarantine/`` with ``state_literal: quarantined``. Both prefixes
share one schema, so one adapter genuinely fits both — building two adapters
for the same source shape would itself be the defect (§2.3, `policy-shared-code`).

The identifier is built from the configured ``id_template``, filled from the
``key_pattern``'s named groups — never console-minted (§2.1). The entries
prefix uses ``event_id`` alone (assumed unique across days); the quarantine
prefix uses ``{day}/{event_id}`` because a quarantined entry's uniqueness is
only guaranteed within its day partition.

Lineage (§3.3/§6): schema-1.0.0's own ``source`` field names the feeder that
wrote the entry (``flow-doctor``, ``changelog-cloudwatch-mirror``,
``changelog-incident-mirror``) — a declared producer id already present in
every body, so a ``produces`` edge is derived from it directly, with no
config needed (unlike `object_store`/`checks_envelope`, whose consumer/
producer ids are declared per-deployment because their sources carry no such
field).

Hermetic: listing and body-reading are two injectable callables so tests run
over recorded fixtures with no live bucket (groom-sweep §8.1).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable

from ..model.entity import Edge, Entity, Provenance
from ..index.build import now_iso
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass
from ..model.kinds import Kind
from ..aws import client as _aws_client

#: A changelog event object is an OBSERVATION (§2.5): it reports something
#: that already happened, exactly once, at a moment in time.
CLAIM_CLASS = ClaimClass.OBSERVATION

name = "changelog-events"
produces = ("incident",)

#: (key, last_modified_iso_or_None)
StoredObject = tuple[str, str | None]
StoreLister = Callable[[str, str], list[StoredObject]]
#: A body reader takes (bucket, key) and returns the decoded event dict, or
#: raises when the object is unreadable.
BodyReader = Callable[[str, str], dict[str, Any]]

_DEFAULT_KEY_PATTERN = r".*/(?P<day>\d{4}-\d{2}-\d{2})/(?P<event_id>[^/]+)\.json$"
_DEFAULT_ID_TEMPLATE = "{event_id}"


def fetch(
    config: dict[str, Any],
    lister: StoreLister | None = None,
    reader: BodyReader | None = None,
    now: datetime | None = None,
) -> AdapterResult:
    bucket = config.get("bucket")
    prefix = config.get("prefix", "")
    pattern = config.get("key_pattern") or _DEFAULT_KEY_PATTERN
    id_template = config.get("id_template") or _DEFAULT_ID_TEMPLATE
    state_field = config.get("state_field")
    state_literal = config.get("state_literal")
    if not bucket:
        return AdapterResult(
            claim_class=CLAIM_CLASS,
            fetched_at=now_iso(),
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
                claim_class=CLAIM_CLASS,
                fetched_at=now_iso(),
                name=config.get("_name", name),
                status=AdapterStatus.FAILED,
                unavailable=tuple(missing),
            )

    regex = re.compile(pattern)
    now = now or datetime.now(timezone.utc)
    source_label = f"s3://{bucket}/{prefix}"

    try:
        objects = lister(bucket, prefix)
    except Exception:
        return AdapterResult(
            claim_class=CLAIM_CLASS,
            fetched_at=now_iso(),
            name=config.get("_name", name),
            status=AdapterStatus.FAILED,
            unavailable=("source",),
        )

    entities: list[Entity] = []
    edges: list[Edge] = []
    partial = False

    for key, last_modified in objects:
        m = regex.search(key)
        if not m:
            continue
        groups = m.groupdict()
        try:
            entity_id = id_template.format(**groups)
        except (KeyError, IndexError):
            entity_id = key  # template referenced a group the pattern didn't name
        if not entity_id:
            continue

        try:
            body = reader(bucket, key)
        except Exception:
            # An unreadable event is a declared absence for that entry — it
            # still appears, so the gap is visible (§2.3), rather than being
            # silently dropped from the corpus.
            entities.append(Entity(
                kind=Kind.INCIDENT,
                id=entity_id,
                state="unreadable",
                provenance=Provenance(
                    source=source_label,
                    as_of=last_modified,
                    evidence=f"s3://{bucket}/{key}",
                ),
                detail={"key": key, "unreadable": True},
            ))
            partial = True
            continue

        state = _state(body, state_field, state_literal)
        ts_utc = body.get("ts_utc")
        flow_doctor = body.get("flow_doctor") or {}
        detail: dict[str, Any] = {
            "ts_utc": ts_utc,
            "event_type": body.get("event_type"),
            "subsystem": body.get("subsystem"),
            "root_cause_category": body.get("root_cause_category"),
            "summary": body.get("summary"),
            "actor": body.get("actor"),
            "source": body.get("source"),
            "error_signature": flow_doctor.get("error_signature"),
            "dedup_count": flow_doctor.get("dedup_count"),
            "key": key,
        }
        validation_errors = body.get("validation_errors")
        if validation_errors is not None:
            detail["validation_errors"] = validation_errors

        entities.append(Entity(
            kind=Kind.INCIDENT,
            id=entity_id,
            state=state,
            provenance=Provenance(
                source=source_label,
                as_of=ts_utc or last_modified,
                evidence=f"s3://{bucket}/{key}",
            ),
            detail=detail,
        ))

        # Declared lineage from the schema's own `source` field (§3.3/§6) — the
        # feeder that wrote this entry, not console-inferred.
        feeder = body.get("source")
        if feeder:
            edges.append(Edge(source=str(feeder), rel="produces", target=entity_id))

    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        name=config.get("_name", name),
        status=AdapterStatus.OK,
        entities=tuple(entities),
        edges=tuple(edges),
        unavailable=("body",) if partial else (),
    )


def _state(
    body: dict[str, Any], state_field: str | None, state_literal: str | None
) -> str:
    """An Incident is not a component (§5.1 second half) — the row carries the
    source's own value, never observability-policy.md §8.3's thirteen.

    ``state_literal`` wins when a whole prefix means one thing regardless of
    the event's own fields (every quarantine object is "quarantined"
    independent of its severity — that IS the fact this pane exists to show).
    Otherwise the configured ``state_field`` is read verbatim and lower-cased.
    Neither configured is a build-time config error the deployment must fix,
    not something this adapter guesses around."""
    if state_literal:
        return str(state_literal)
    if state_field:
        val = body.get(state_field)
        if val:
            return str(val).strip().lower()
    return "unspecified"


def _default_s3() -> tuple[StoreLister | None, BodyReader | None]:
    """boto3-backed lister + body reader when the optional AWS extra is installed.

    Returns ``(None, None)`` when boto3 is absent so the adapter fails loud
    rather than silently returning zero rows (§5.5).
    """
    try:
        import json

        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ImportError:
        return None, None

    def lister(bucket: str, prefix: str) -> list[StoredObject]:
        client = _aws_client("s3")
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
                stamp = lm.isoformat() if hasattr(lm, "isoformat") else (str(lm) if lm else None)
                out.append((key, stamp))
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        return out

    def reader(bucket: str, key: str) -> dict[str, Any]:
        client = _aws_client("s3")
        try:
            resp = client.get_object(Bucket=bucket, Key=key)
            body = resp["Body"].read()
        except (BotoCoreError, ClientError):
            raise
        return json.loads(body.decode("utf-8"))

    return lister, reader
