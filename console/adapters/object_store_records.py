"""`object-store-records` adapter — many typed entities from ONE JSON body
(`console-policy.md` §2.3).

Complementary to `object-store` (a key -> one Artifact, from listing metadata
alone) and `checks-envelope` (a key -> Component/Run/Artifact, from a fixed
check-result envelope shape): this adapter reads an explicitly-named body,
finds a configured list of records inside it, and projects EACH RECORD into
its own entity — the shape a snapshot artifact takes when its value is a
scored universe (one row per ticker) rather than a single fact about the
object itself. Kept as its own adapter for the same reason `checks-envelope`
is kept separate from `object-store`: "same source shape, a different
projection" (§2.3 — one source shape per adapter, and reading a body's
*content* is a different shape of read than reading a body's *existence*).

    keys: ["scanner/universe/latest.json"]
    entity_kind: artifact
    records_path: stocks                 # dotted path to the list within the body
    id_template: "{ticker}"              # formatted against each record
    body_as_of_field: as_of              # top-level field, injected into every
                                          # record's format context under `as_of`
                                          # (only when the record has no such key)
    state_field: gate.quant_filter_pass  # dotted path within the record
    facets: {sector: sector}

Nothing about a record's structure is assumed beyond "a JSON object, in a
list, inside a JSON body" — nested sub-objects (a record's own `gate`,
`pillars`, `metrics`) pass into `detail` untouched; the console renders
declared/opaque data generically (§5.8) rather than requiring every adapter
to flatten it first.

Only raw-value kinds (`artifact`, `signal`, `decision`, `incident`, `cycle`)
are accepted: a record's status here is never resolvable to §8.3's closed
twelve-state vocabulary without a config-declared mapping this adapter has no
generic way to express for arbitrary nested JSON, so `component`/`run` are
rejected at fetch time with a named config error rather than crashing later
on `model/entity.py`'s state-type invariant.

Hermetic: the body read is one injectable function so tests run over recorded
JSON fixtures with no live bucket (groom-sweep §8.1).
"""
from __future__ import annotations

import json
from typing import Any, Callable

from ..model.entity import Entity, Provenance
from ..index.build import now_iso
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass
from ..model.kinds import COMPONENT_STATE_KINDS, Kind

#: A snapshot body is an OBSERVATION (§2.5): what the source currently holds.
CLAIM_CLASS = ClaimClass.OBSERVATION

name = "object-store-records"
produces = ("artifact", "signal", "decision", "incident", "cycle")

#: A body reader takes (bucket, key) and returns the decoded JSON body.
BodyReader = Callable[[str, str], Any]


def fetch(
    config: dict[str, Any],
    reader: BodyReader | None = None,
) -> AdapterResult:
    bucket = config.get("bucket")
    keys = config.get("keys") or ([config["key"]] if config.get("key") else [])
    entity_kind = _kind(config.get("entity_kind"))
    records_path = config.get("records_path")
    id_template = config.get("id_template")

    missing = [
        n for n, v in (
            ("bucket", bucket), ("keys", keys),
            ("records_path", records_path), ("id_template", id_template),
        ) if not v
    ]
    if entity_kind is None:
        missing.append("entity_kind")
    elif entity_kind in COMPONENT_STATE_KINDS:
        return _failed(
            config,
            f"entity_kind:{entity_kind.value} — this adapter emits raw-value "
            "kinds only; a component-state kind needs a declared state_map "
            "this generic record shape has no way to express",
        )
    if missing:
        return _failed(config, ",".join(missing))

    if reader is None:
        reader = _default_reader()
        if reader is None:
            return _failed(config, "reader")

    body_as_of_field = config.get("body_as_of_field", "as_of")
    state_field = config.get("state_field")
    default_state = config.get("default_state", "reporting")
    facets_cfg = config.get("facets") or {}
    evidence_template = config.get("evidence_template")

    entities: list[Entity] = []
    unavailable: list[str] = []
    read_any = False

    for key in keys:
        try:
            body = reader(str(bucket), str(key))
        except Exception:  # noqa: BLE001 - a failed source is a state, never an exception (§2.3)
            unavailable.append(str(key))
            continue
        read_any = True

        records = _get_path(body, records_path)
        if not isinstance(records, list):
            unavailable.append(f"{key}:records_path")
            continue

        body_as_of = body.get(body_as_of_field) if isinstance(body, dict) else None
        source_label = f"s3://{bucket}/{key}"

        for record in records:
            if not isinstance(record, dict):
                continue
            fmt_ctx = dict(record)
            if body_as_of is not None:
                fmt_ctx.setdefault("as_of", body_as_of)
            try:
                entity_id = str(id_template).format(**fmt_ctx)
            except (KeyError, IndexError):
                continue

            state = _get_path(record, state_field) if state_field else None
            state_str = str(state) if state is not None else str(default_state)

            evidence = source_label
            if evidence_template:
                try:
                    evidence = str(evidence_template).format(bucket=bucket, key=key, **fmt_ctx)
                except (KeyError, IndexError):
                    evidence = source_label

            facets: dict[str, str] = {}
            for facet_name, path in facets_cfg.items():
                value = _get_path(record, path)
                if value is not None:
                    facets[str(facet_name)] = str(value)

            entities.append(Entity(
                kind=entity_kind,
                id=entity_id,
                state=state_str,
                provenance=Provenance(
                    source=source_label,
                    as_of=str(body_as_of) if body_as_of is not None else None,
                    evidence=evidence,
                ),
                facets=facets,
                detail=dict(record),
            ))

    if not read_any:
        return _failed(config, "source")

    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        name=config.get("_name", name),
        status=AdapterStatus.OK,
        entities=tuple(entities),
        unavailable=tuple(dict.fromkeys(unavailable)),
    )


def _kind(raw: Any) -> Kind | None:
    if not raw:
        return None
    try:
        return Kind(str(raw).lower())
    except ValueError:
        return None


def _get_path(d: Any, path: str | None) -> Any:
    if not path:
        return None
    cur = d
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
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


def _default_reader() -> BodyReader | None:
    """boto3-backed reader when the optional `aws` extra is installed."""
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError:
        return None

    client = boto3.client("s3")

    def reader(bucket: str, key: str) -> Any:
        resp = client.get_object(Bucket=bucket, Key=key)
        return json.loads(resp["Body"].read().decode("utf-8"))

    return reader
