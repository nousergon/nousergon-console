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

Hermetic: listing and body-reading are two injectable callables so tests run
over recorded fixtures with no live bucket (groom-sweep §8.1).
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Any, Callable

from ..model.entity import Edge, Entity, Provenance
from ..index.build import now_iso
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass
from ..model.kinds import COMPONENT_STATE_KINDS, Kind, State
from .object_store import _parse_cadence  # second adoption, one repo — see below

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

    ``body_root`` is what field paths resolve against for whole-body /
    body-level fields; each record is merged on top of it per-entity (record
    wins on key collision) so a per-ticker field and a per-day field are both
    reachable through the same ``path`` syntax.
    """
    if fmt == "csv":
        text = body if isinstance(body, str) else body.decode("utf-8")
        rows = list(csv.DictReader(io.StringIO(text)))
        return rows, {}

    parsed = json.loads(body) if isinstance(body, (str, bytes)) else body
    if not isinstance(parsed, dict):
        raise TypeError("s3-records JSON body must be an object")

    records_path = config.get("records_path")
    array_fields = config.get("array_fields")
    if records_path:
        parts = str(records_path).split(".")
        if "*" in parts:
            return _explode_grouped(parsed, parts, config.get("group_field")), parsed
        raw_list = _get_path(parsed, records_path)
        if not isinstance(raw_list, list):
            raise TypeError(f"records_path {records_path!r} is not a list")
        return [r for r in raw_list if isinstance(r, dict)], parsed
    if array_fields:
        arrays = [parsed.get(f) for f in array_fields]
        if any(not isinstance(a, list) for a in arrays):
            raise TypeError("array_fields must all name JSON arrays")
        rows = [
            {field: values[i] for field, values in zip(array_fields, arrays)}
            for i in range(min(len(a) for a in arrays))
        ]
        return rows, parsed
    # Whole-body mode: the object IS the one record.
    return [{}], parsed


def _explode_grouped(cur: Any, parts: list[str], group_field: str | None) -> list[dict]:
    """Resolve a `records_path` containing a `*` segment (module docstring's
    "Grouped" shape). `*` iterates a dict's keys — rather than indexing a
    named key like every other segment — injecting each key under
    `group_field` into every record reached beneath it. The terminal value
    may be a list (each dict item becomes one record) or a dict (a
    dict-of-records: each VALUE becomes one record, its key injected the
    same way `*` injects one mid-path) — both are "a dict names several
    records" at different depths, so one walk covers both."""
    out: list[dict] = []
    _walk_grouped(cur, parts, group_field, out)
    return out


def _walk_grouped(cur: Any, parts: list[str], group_field: str | None, out: list[dict]) -> None:
    if not parts:
        if isinstance(cur, dict):
            out.append(cur)
        elif isinstance(cur, list):
            out.extend(item for item in cur if isinstance(item, dict))
        return
    part, rest = parts[0], parts[1:]
    if part == "*":
        if not isinstance(cur, dict):
            return
        for key, value in cur.items():
            before = len(out)
            _walk_grouped(value, rest, group_field, out)
            for rec in out[before:]:
                rec.setdefault(group_field or "group", key)
        return
    if isinstance(cur, dict) and part in cur:
        _walk_grouped(cur[part], rest, group_field, out)


def _get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


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
    flat_context = {
        **groups,
        **{k: v for k, v in body_root.items() if not isinstance(v, (dict, list))},
        **{k: v for k, v in record.items() if not isinstance(v, (dict, list))},
    }

    id_template = config.get("id_template", "{" + "}{".join(groups) + "}" if groups else "")
    try:
        entity_id = str(id_template).format(**flat_context)
    except (KeyError, IndexError):
        return None
    if not entity_id:
        return None

    as_of_field = config.get("as_of_field")
    as_of = str(_get_path(path_root, as_of_field)) if as_of_field and _get_path(path_root, as_of_field) is not None else last_modified

    evidence_template = config.get("evidence_template")
    evidence = (
        str(evidence_template).format(**flat_context) if evidence_template
        else f"s3://{bucket}/{key}"
    )

    state = _resolve_state(kind, config, path_root, as_of, cadence_seconds, staleness_factor, now)

    fields_out: dict[str, dict[str, Any]] = {}
    question = config.get("question")
    if question:
        fields_out["question"] = {"value": str(question), "render": "text"}
    for fname, spec in (config.get("fields") or {}).items():
        val = _get_path(path_root, spec.get("path")) if spec.get("path") else None
        entry: dict[str, Any] = {"value": val, "render": spec.get("render", "value")}
        if spec.get("unit") is not None:
            entry["unit"] = spec["unit"]
        if "baseline" in spec:
            entry["baseline"] = spec["baseline"]
        fields_out[fname] = entry

    return Entity(
        kind=kind,
        id=entity_id,
        state=state,
        provenance=Provenance(source=source_label, as_of=as_of, evidence=evidence),
        detail={"fields": fields_out, "key": key},
    )


def _resolve_state(
    kind: Kind,
    config: dict[str, Any],
    path_root: dict,
    as_of: str | None,
    cadence_seconds: float | None,
    staleness_factor: float,
    now: datetime,
) -> State | str:
    state_field = config.get("state_field")
    raw = _get_path(path_root, state_field) if state_field else None
    if raw is None:
        raw = config.get("state_default")

    if kind in COMPONENT_STATE_KINDS:
        # §5.1: Component/Run MUST carry one of the twelve — never a raw
        # passthrough (Entity.__post_init__ enforces this structurally).
        if raw is None:
            return State.UNREPORTED
        try:
            return State[str(raw).upper()]
        except KeyError:
            return State.UNREPORTED

    if raw is not None:
        return str(raw)

    # No domain state declared — fall back to the object_store convention:
    # freshness relative to a declared cadence renders the value itself
    # (§5.1's second half), and the three not-computable cases stay three
    # facts (§5.5) rather than collapsing into one token.
    if as_of is None:
        return "no-freshness-stamp"
    if cadence_seconds is None:
        return "no-cadence-declared"
    try:
        ts = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
    except ValueError:
        return "unreadable"
    age = (now - ts).total_seconds()
    return "fresh" if age <= cadence_seconds * staleness_factor else "stale"


def _default_s3() -> tuple[StoreLister | None, BodyReader | None]:
    """boto3-backed lister + body reader when the optional AWS extra is installed."""
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
                stamp = lm.isoformat() if hasattr(lm, "isoformat") else (str(lm) if lm else None)
                out.append((key, stamp))
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        return out

    def reader(bucket: str, key: str) -> Any:
        client = boto3.client("s3")
        try:
            resp = client.get_object(Bucket=bucket, Key=key)
            raw = resp["Body"].read()
        except (BotoCoreError, ClientError):
            raise
        if key.endswith(".csv"):
            return raw.decode("utf-8")
        return json.loads(raw.decode("utf-8"))

    return lister, reader
