"""changelog-retro-feed adapter — Incident (grouped) entities from the
deterministic retro-candidate aggregate.

Reads ONE JSON document (``changelog/retro_candidates.json``, emitted daily by
`alpha-engine-docs`' ``aggregate-changelog.yml`` cron) whose ``incident_groups``
array already carries the upstream grouping this pane answers its declared
question with: which recurring incidents ((subsystem, normalized summary))
are ready for a written retro, and which still need triage
(`console-policy.md` §2.1, ``nousergon-console#60``).

Why a dedicated adapter rather than ``changelog_events``: that adapter's shape
is "one S3 key per entity" (§2.3 — the object-store family). This source is
the opposite shape — **one key holds many entities**, pre-grouped by an
upstream aggregator — so it is a different projection and a different adapter,
the same reasoning `checks_envelope` gives for not extending `object_store`.

No filter logic is migrated here: the upstream aggregator
(``alpha-engine-docs/scripts/emit_retro_candidates.py``, guarded by the
changelog-incident-mirror classifier, PR #378) already excludes SUCCESS/OK
entries before this document is written. This adapter trusts that filter and
projects what it is given — confirming the dependency still holds is a
data-pipeline fact, not something this adapter's code enforces (the source
view carried no filter of its own either; see `28_Retros.py`).

The identifier is ``{subsystem}|{summary}`` — both fields are already the
upstream-normalized values (they are the group key the aggregator itself
grouped by), so no further normalization happens here (§2.1 — never
console-minted).

A ``ready_for_retro`` entry with a writeup is merged into its matching group
by the same (subsystem, summary) key, carrying the narrative fields into
``detail.resolution``. A ``ready_for_retro`` entry with no matching group
(should not happen given the emitter's own invariant — every writeup implies
a group) is not separately surfaced; this is a documented limitation, not a
silent drop, because the emitter guarantees the invariant this adapter relies
on.

Hermetic: body-reading is one injectable callable so tests run over a
recorded fixture with no live bucket (groom-sweep §8.1).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ..model.entity import Entity, Provenance
from ..index.build import now_iso
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass
from ..model.kinds import Kind

#: The aggregate is an OBSERVATION (§2.5) — a computed rollup of what the
#: event lake already recorded, not a declaration of intent.
CLAIM_CLASS = ClaimClass.OBSERVATION

name = "changelog-retro-feed"
produces = ("incident",)

#: A reader takes (bucket, key) and returns the decoded document, or raises
#: when the object is unreadable/absent.
BodyReader = Callable[[str, str], dict[str, Any]]


def fetch(
    config: dict[str, Any],
    reader: BodyReader | None = None,
    now: datetime | None = None,
) -> AdapterResult:
    bucket = config.get("bucket")
    key = config.get("key")
    if not bucket or not key:
        return AdapterResult(
            claim_class=CLAIM_CLASS,
            fetched_at=now_iso(),
            name=config.get("_name", name),
            status=AdapterStatus.FAILED,
            unavailable=("all",),
        )
    if reader is None:
        reader = _default_reader()
        if reader is None:
            return AdapterResult(
                claim_class=CLAIM_CLASS,
                fetched_at=now_iso(),
                name=config.get("_name", name),
                status=AdapterStatus.FAILED,
                unavailable=("reader",),
            )

    now = now or datetime.now(timezone.utc)
    source_label = f"s3://{bucket}/{key}"

    try:
        body = reader(bucket, key)
    except Exception:
        return AdapterResult(
            claim_class=CLAIM_CLASS,
            fetched_at=now_iso(),
            name=config.get("_name", name),
            status=AdapterStatus.FAILED,
            unavailable=("source",),
        )

    generated_at = body.get("generated_at")
    groups = body.get("incident_groups") or []
    ready = body.get("ready_for_retro") or []
    ready_by_key = {
        (r.get("subsystem"), r.get("summary")): r
        for r in ready
        if isinstance(r, dict)
    }

    entities: list[Entity] = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        subsystem = g.get("subsystem") or "unknown"
        summary = g.get("summary") or ""
        entity_id = f"{subsystem}|{summary}"
        has_writeup = bool(g.get("has_writeup"))
        # Not a component (§5.1 second half): "ready for retro" / "needs
        # triage" is what this pane's declared question actually asks, and
        # forcing it into observability-policy.md §8.3's twelve would answer
        # a question nobody asked.
        state = "ready-for-retro" if has_writeup else "needs-triage"

        detail: dict[str, Any] = {
            "severity": g.get("severity"),
            "count": g.get("count"),
            "latest_ts": g.get("latest_ts"),
            "has_writeup": has_writeup,
        }
        match = ready_by_key.get((subsystem, summary))
        if match:
            detail["resolution"] = {
                "root_cause_category": match.get("root_cause_category"),
                "resolution_type": match.get("resolution_type"),
                "resolution_notes": match.get("resolution_notes"),
                "git_refs": match.get("git_refs"),
                "ts_utc": match.get("ts_utc"),
            }

        entities.append(Entity(
            kind=Kind.INCIDENT,
            id=entity_id,
            state=state,
            provenance=Provenance(
                source=source_label,
                as_of=g.get("latest_ts") or generated_at,
                evidence=source_label,
            ),
            detail=detail,
        ))

    cadence = _parse_cadence(config.get("cadence"))
    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        declared_cadence_seconds=cadence,
        name=config.get("_name", name),
        status=AdapterStatus.OK,
        entities=tuple(entities),
    )


def _parse_cadence(cadence: Any) -> float | None:
    """'1d' → 86400, '90m' → 5400, '300' → 300. None when undeclared."""
    if cadence is None:
        return None
    if isinstance(cadence, (int, float)):
        return float(cadence)
    s = str(cadence).strip()
    if not s:
        return None
    unit = s[-1]
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit)
    if mult is None:
        return None
    try:
        return float(s[:-1]) * mult
    except ValueError:
        return None


def _default_reader() -> BodyReader | None:
    """boto3-backed single-object reader when the optional AWS extra is
    installed. Returns None when boto3 is absent so the adapter fails loud
    rather than silently returning zero rows (§5.5)."""
    try:
        import json

        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ImportError:
        return None

    def reader(bucket: str, key: str) -> dict[str, Any]:
        client = boto3.client("s3")
        try:
            resp = client.get_object(Bucket=bucket, Key=key)
            body = resp["Body"].read()
        except (BotoCoreError, ClientError):
            raise
        return json.loads(body.decode("utf-8"))

    return reader
