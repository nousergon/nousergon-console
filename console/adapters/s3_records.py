"""s3-records adapter — configurable-kind entities from S3 JSON/CSV bodies.

Generic over "an S3-compatible prefix whose objects carry zero, one, or many
per-instance records, each projected onto a **configured** entity kind"
(`config.example.yaml`'s `s3-records` block). This is the adapter for a source
the console does not control the shape of but that already carries everything
a row needs — one legacy dashboard's per-artifact JSON/CSV, read the same way
its own consumer reads it, never mutated in place (nousergon-console#54).

Three source shapes are covered by ONE adapter because they are the same
underlying pattern — "a key's body names zero-or-more records" — with three
different ways a body expresses that:

- **Whole-body** (no ``records_path``/``array_fields``): the object IS the one
  record (`consolidated/{date}/eod_report.json` → one Cycle per date).
- **List-of-dicts** (``records_path``, a dotted path to a JSON array of
  objects): fan out one entity per list item (`order_book_rationale`'s
  ``tickers`` array → one Decision per ticker).
- **Grouped** (``records_path`` containing a ``*`` segment, plus
  ``group_field``): a ``*`` iterates a DICT at that point in the path rather
  than indexing a named key, injecting its key under ``group_field`` into
  every record it reaches — covers a nested dict-then-array
  (``"tiles.*.components"``: a report card's per-tile MetricRecords, the
  tile name injected onto each) and a dict OF records
  (``"loops.*"``: an apply-audit's per-loop outcomes, the loop id injected)
  with one mechanism, since neither is a JSON array at any single dotted
  path the plain ``records_path`` case above can reach
  (`nousergon-console#57`).
- **Parallel arrays** (``array_fields``, a list of equal-length array field
  names): zip index-wise into one record per index (`optimizer_shadow`'s
  ``tickers``/``target_weights``/… arrays → one Decision per ticker with no
  per-ticker object in the source at all).
- **CSV** (``format: csv``): each row is a record (`trades_full.csv` → one Run
  per trade). The whole file is a record list; ``records_path``/``array_fields``
  do not apply.

Every field beyond id/state/provenance is declared in config (§5.8) — a
``{field_name: {path, unit, render, baseline}}`` map resolved against the
merged record+body structure — so nothing about a specific source's business
meaning (which JSON key means what) is compiled into this module. The
``question`` config key (`console-policy.md` §4.4) is carried through as a
synthetic ``text`` declared field so the pane renders it without any
kind-specific rendering code.

**Component/Run state** comes from ``state_field`` (a dotted path), resolved in
this order: an optional ``state_map`` translating the source's own vocabulary
(``{"passed": "HEALTHY", "failed": "FAILED"}``) into
`observability-policy.md` §8.3's thirteen, then a direct match on a state name.
Three outcomes stay three facts (§5.5): **no value** renders ``UNREPORTED``
(nothing reported), a value **nothing can interpret** renders ``DEGRADED``
(something reported, uninterpretable — a finding), and a ``state_map`` entry
naming a state that does not exist also renders ``DEGRADED``, because a typo in
the map must never read as healthy.

**One adapter, one source shape (§2.3).** Three sibling adapters implementing
this same shape — ``object-store-records``, ``dated-snapshot`` and this one —
were built within an hour by concurrent sessions on 2026-08-10, none able to see
another's in-flight branch. Consolidated onto this module by Brian's ruling of
2026-08-11 (`nousergon-console#79`): ``object-store-records``' explicit key list
is a ``key_pattern`` matching one literal, and ``dated-snapshot``'s ``state_map``
is folded in above. Two record-shaped adapters remain deliberately separate and
are NOT candidates to fold here — see `docs/adapters.md` for the boundary test.

Hermetic: listing and body-reading are two injectable callables so tests run
over recorded fixtures with no live bucket (groom-sweep §8.1).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from ..model.entity import Edge, Entity, Provenance
from ..index.build import now_iso
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass
from ..model.kinds import Kind
from ..records_shape import (
    build_fields, flat_context, get_path, project, resolve_id, resolve_state,
)
from .object_store import _parse_cadence  # second adoption, one repo — see below
from ..aws import client as _aws_client

#: A dashboard's own read of its S3 artifacts is an OBSERVATION (§2.5): it
#: says what the artifact currently contains, never a decision about it.
CLAIM_CLASS = ClaimClass.OBSERVATION

name = "s3-records"
#: Fully generic over `kind` — every §2.1 kind is reachable depending on config.
produces = ("component", "run", "cycle", "artifact", "signal", "decision", "incident")

#: (key, last_modified_iso_or_None)
StoredObject = tuple[str, str | None]
StoreLister = Callable[[str, str], list[StoredObject]]
#: A body reader takes (bucket, key) and returns the decoded body: a dict for
#: JSON, or the raw text for CSV. Raises when the object is unreadable.
BodyReader = Callable[[str, str], Any]


def fetch(
    config: dict[str, Any],
    lister: StoreLister | None = None,
    reader: BodyReader | None = None,
    now: datetime | None = None,
) -> AdapterResult:
    bucket = config.get("bucket")
    prefix = config.get("prefix", "")
    pattern = config.get("key_pattern")
    kind = _resolve_kind(config.get("kind"))
    if not bucket or not pattern or kind is None:
        missing = [n for n, v in (("bucket", bucket), ("key_pattern", pattern)) if not v]
        if kind is None:
            missing.append("kind")
        return _failed(config, tuple(missing) or ("all",))
    if lister is None or reader is None:
        default_lister, default_reader = _default_s3()
        lister = lister or default_lister
        reader = reader or default_reader
        missing = [n for n, v in (("lister", lister), ("reader", reader)) if v is None]
        if missing:
            return _failed(config, tuple(missing))

    import re

    regex = re.compile(pattern)
    fmt = config.get("format", "json")
    staleness_factor = float(config.get("staleness_factor", 1.5))
    cadence_seconds = _parse_cadence(config.get("cadence"))
    now = now or datetime.now(timezone.utc)
    source_label = f"s3://{bucket}/{prefix}"

    try:
        objects = lister(bucket, prefix)
    except Exception:
        return AdapterResult(
            claim_class=CLAIM_CLASS, fetched_at=now_iso(), name=config.get("_name", name),
            status=AdapterStatus.FAILED, unavailable=("source",),
        )

    entities: list[Entity] = []
    edges: list[Edge] = []
    partial = False

    for key, last_modified in objects:
        m = regex.search(key)
        if not m:
            continue
        groups = {k: v for k, v in m.groupdict().items() if v is not None}
        try:
            body = reader(bucket, key)
        except Exception:
            partial = True
            continue

        try:
            records, body_root = _project(body, fmt, config)
        except (TypeError, ValueError, KeyError):
            # A body that does not match its declared shape is a finding about
            # THIS key, not a reason to drop the whole adapter's pass (§2.3).
            partial = True
            continue

        for record in records:
            mapped = _one_entity(
                record, body_root, groups, kind, key, bucket, source_label,
                last_modified, staleness_factor, cadence_seconds, now, config,
            )
            if mapped is None:
                partial = True
                continue
            entities.append(mapped)

        cid = groups.get("component_id")
        if cid:
            edges.append(Edge(source=cid, rel="produces", target=key))

    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        declared_cadence_seconds=cadence_seconds,
        name=config.get("_name", name),
        status=AdapterStatus.OK,
        entities=tuple(entities),
        edges=tuple(edges),
        unavailable=("body",) if partial else (),
    )


def _failed(config: dict[str, Any], missing: tuple[str, ...]) -> AdapterResult:
    return AdapterResult(
        claim_class=CLAIM_CLASS, fetched_at=now_iso(), name=config.get("_name", name),
        status=AdapterStatus.FAILED, unavailable=missing,
    )


def _resolve_kind(raw: Any) -> Kind | None:
    if not raw:
        return None
    try:
        return Kind(str(raw))
    except ValueError:
        return None


def _project(body: Any, fmt: str, config: dict[str, Any]) -> tuple[list[dict], dict]:
    """Turn one key's body into (records, body_root) per the declared shape.
    Thin wrapper over the shared grammar (`console/records_shape.py`) — this
    adapter's only job is picking the config keys off ITS config dict; the
    grammar itself is shared with the `s3-records` driver (§2.3).
    """
    return project(body, fmt, config.get("records_path"), config.get("array_fields"),
                    config.get("group_field"))


def _one_entity(
    record: dict,
    body_root: dict,
    groups: dict[str, str],
    kind: Kind,
    key: str,
    bucket: str,
    source_label: str,
    last_modified: str | None,
    staleness_factor: float,
    cadence_seconds: float | None,
    now: datetime,
    config: dict[str, Any],
) -> Entity | None:
    path_root = {**body_root, **record}
    context = flat_context(groups, body_root, record)

    id_template = config.get("id_template", "{" + "}{".join(groups) + "}" if groups else "")
    entity_id = resolve_id(id_template, context)
    if entity_id is None:
        return None

    as_of_field = config.get("as_of_field")
    as_of = str(get_path(path_root, as_of_field)) if as_of_field and get_path(path_root, as_of_field) is not None else last_modified

    evidence_template = config.get("evidence_template")
    evidence = (
        str(evidence_template).format(**context) if evidence_template
        else f"s3://{bucket}/{key}"
    )

    state = resolve_state(kind, config.get("state_field"), config.get("state_default"),
                           config.get("state_map"), path_root, as_of, cadence_seconds,
                           staleness_factor, now)

    fields_out = build_fields(path_root, config.get("fields"), config.get("question"))

    # Facets are what §2.2 filters on uniformly across the whole index, so they
    # are a different thing from declared `fields` (§5.8), which are rendered.
    # Folded in from `object-store-records` during the I79 consolidation — it
    # was that adapter's second real capability, alongside its explicit key
    # list. A facet whose path resolves to nothing is OMITTED rather than
    # written as an empty string: an absent facet and a facet whose value is ""
    # filter differently, and inventing the second is a fabricated fact.
    facets: dict[str, str] = {}
    for facet_name, facet_path in (config.get("facets") or {}).items():
        value = get_path(path_root, str(facet_path))
        if value is not None:
            facets[str(facet_name)] = str(value)

    return Entity(
        kind=kind,
        id=entity_id,
        state=state,
        provenance=Provenance(source=source_label, as_of=as_of, evidence=evidence),
        facets=facets,
        detail={"fields": fields_out, "key": key},
    )



def _default_s3() -> tuple[StoreLister | None, BodyReader | None]:
    """boto3-backed lister + body reader when the optional AWS extra is installed."""
    try:
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

    def reader(bucket: str, key: str) -> Any:
        client = _aws_client("s3")
        try:
            resp = client.get_object(Bucket=bucket, Key=key)
            raw = resp["Body"].read()
        except (BotoCoreError, ClientError):
            raise
        if key.endswith(".csv"):
            return raw.decode("utf-8")
        return json.loads(raw.decode("utf-8"))

    return lister, reader
