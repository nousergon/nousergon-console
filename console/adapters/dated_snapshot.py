"""`dated-snapshot` adapter — one JSON document per date/version under a
prefix becomes one Signal or Run entity with declared fields (§2.3).

Generic over "a family of per-date or per-version JSON snapshots" — the shape
a champion/challenger leaderboard, a weekly training-summary artifact, or a
regime-substrate snapshot all share: `object-store` already enumerates such a
prefix for FRESHNESS alone (a key becomes an Artifact, staleness from
last-modified). This adapter reads each matched object's CONTENT and projects
declared fields onto a **typed** entity, reusing the same lister injection
`object-store` established plus a `document_reader` injection (the same
capability `document-fields`, the driver-side sibling, uses for content).

Kind is **configured**, never guessed (§2.3): `run` resolves to
`observability-policy.md` §8.3's twelve-state vocabulary
(`model/kinds.py::COMPONENT_STATE_KINDS`); `signal` carries the source's own
value verbatim (§5.1's second half — a Signal is not HEALTHY or FAILED, it is
"reporting" or a domain value, exactly like `pipeline-reliability`'s Cycle
states are not §8.3 members either).

For `kind: run`, `state_field` + `state_map` are **required** and declared in
config — never hardcoded (§2.3): config maps one raw JSON value to one of the
twelve. An unmapped value renders `DEGRADED` with the raw value kept on the
entity's `detail`, never a fabricated green — the same "never guess health"
discipline `document-fields` applies to a Component's own freshness, applied
here to a business verdict a config-declared map, not adapter code, owns.

For `kind: signal`, `state_field` is optional; its raw value (lower-cased)
becomes the entity's own state string, or `"reporting"` when the document was
read but no state field was declared — matching `pipeline-reliability`'s
`_first_attempt_signal` convention exactly.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable

from ..index.build import now_iso
from ..model.entity import Entity, Provenance
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass
from ..model.kinds import Kind, State

#: A dated snapshot is OBSERVATION (§2.5) — what a producer wrote, and when.
CLAIM_CLASS = ClaimClass.OBSERVATION

name = "dated-snapshot"
produces = ("signal", "run")

#: One stored object: its key and an optional ISO last-modified stamp — same
#: shape `object-store` uses, so the same lister can serve both.
StoredObject = tuple[str, str | None]
StoreLister = Callable[[str, str], list[StoredObject]]
DocumentReader = Callable[[str], Any]

_KIND_MAP: dict[str, Kind] = {"signal": Kind.SIGNAL, "run": Kind.RUN}


def fetch(
    config: dict[str, Any],
    lister: StoreLister | None = None,
    document_reader: DocumentReader | None = None,
    now: datetime | None = None,
) -> AdapterResult:
    bucket = config.get("bucket")
    prefix = config.get("prefix", "")
    pattern = config.get("key_pattern")
    kind_name = config.get("kind")
    id_template = config.get("id_template")

    if not bucket or not pattern or not id_template:
        return _failed(config, "config")
    if kind_name not in _KIND_MAP:
        return _failed(config, "kind")
    kind = _KIND_MAP[kind_name]

    if lister is None:
        lister = _default_lister()
        if lister is None:
            return _failed(config, "lister")
    if document_reader is None:
        document_reader = _default_document_reader(bucket)
        if document_reader is None:
            return _failed(config, "document_reader")

    regex = re.compile(pattern)
    cadence = _parse_cadence(config.get("cadence"))
    field_map: dict[str, Any] = config.get("fields") or {}
    state_field = config.get("state_field")
    state_map: dict[str, str] = config.get("state_map") or {}
    now = now or datetime.now(timezone.utc)

    try:
        objects = lister(bucket, prefix)
    except Exception:
        return _failed(config, "source")

    entities: list[Entity] = []
    for key, last_modified in objects:
        m = regex.match(key)
        if not m:
            continue
        groups = m.groupdict()
        try:
            entity_id = id_template.format(**groups)
        except (KeyError, IndexError):
            continue

        try:
            body = document_reader(key)
        except Exception:
            # A broken individual snapshot is excluded from this pass, not a
            # source-wide fetch failure — a hundred other snapshots are fine.
            continue
        if not isinstance(body, dict):
            continue

        fields: dict[str, Any] = {}
        for out_name, spec in field_map.items():
            value = _extract(body, spec.get("path", out_name))
            entry: dict[str, Any] = {"value": value}
            if "unit" in spec:
                entry["unit"] = spec["unit"]
            if "baseline" in spec:
                entry["baseline"] = spec["baseline"]
            if "render" in spec:
                entry["render"] = spec["render"]
            fields[out_name] = entry

        raw_state = _extract(body, state_field) if state_field else None
        state = _resolve_state(kind, raw_state, state_map)

        entities.append(Entity(
            kind=kind,
            id=entity_id,
            state=state,
            provenance=Provenance(
                source=f"dated-snapshot:{bucket}/{prefix}",
                as_of=last_modified,
                evidence=f"s3://{bucket}/{key}",
            ),
            facets={k: v for k, v in groups.items() if v is not None},
            detail={
                "fields": fields,
                **({"raw_state": raw_state} if state_field else {}),
            },
        ))

    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        declared_cadence_seconds=cadence,
        name=config.get("_name", name),
        status=AdapterStatus.OK,
        entities=tuple(entities),
    )


def _resolve_state(kind: Kind, raw_state: Any, state_map: dict[str, str]) -> State | str:
    if kind is Kind.SIGNAL:
        return str(raw_state).lower() if raw_state is not None else "reporting"
    # kind is RUN — must resolve to a §8.3 State, never a raw string (§5.1).
    mapped_name = state_map.get(str(raw_state)) if raw_state is not None else None
    if mapped_name is not None:
        try:
            return State[mapped_name]
        except KeyError:
            return State.DEGRADED
    # Unmapped or absent: never a fabricated green. The raw value is kept on
    # `detail.raw_state` so the reason is visible, not just the verdict.
    return State.DEGRADED


def _extract(body: dict[str, Any], path: str) -> Any:
    cur: Any = body
    for part in str(path).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _failed(config: dict[str, Any], missing: str) -> AdapterResult:
    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        name=config.get("_name", name),
        status=AdapterStatus.FAILED,
        unavailable=(missing,),
    )


def _parse_cadence(cadence: Any) -> float | None:
    """'1h' -> 3600, '30m' -> 1800, '300' -> 300. None when undeclared."""
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


def _default_lister() -> StoreLister | None:
    """boto3-backed lister — identical projection to `object-store`'s."""
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
                stamp = lm.isoformat() if hasattr(lm, "isoformat") else (str(lm) if lm else None)
                out.append((key, stamp))
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        return out

    return lister


def _default_document_reader(bucket: str) -> DocumentReader | None:
    """boto3-backed `get_object` + JSON parse, bound to this fetch's bucket."""
    try:
        import boto3  # type: ignore
    except ImportError:
        return None

    client = boto3.client("s3")

    def reader(key: str) -> Any:
        import json
        obj = client.get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read())

    return reader
