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
- **Listing only** (``format: object``): the body is never read; the key, its
  last-modified stamp and the key pattern's named groups are the one record
  (a dated markdown report → one Run per date). ``records_path``/
  ``array_fields`` are refused.
  With ``body: text`` the object's text is carried beside that record as
  ``detail["document"]`` and shown on the entity page (``body: markdown``
  carries it the same way and the page renders it as markdown); ``window_days`` has a
  list of the rows open on its last N days with an archive one link away.

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
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable

from ..model.entity import DOCUMENT_DETAIL, DOCUMENT_MARKDOWN, WINDOW_DETAIL, Edge, Entity, Provenance
from ..index.build import now_iso
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass
from ..model.kinds import Kind
from ..records_shape import (
    build_fields, flat_context, get_path, project, resolve_facets, resolve_id,
    resolve_state,
)
from .object_store import _parse_cadence  # second adoption, one repo — see below
from ..aws import client as _aws_client
from ..s3_body_cache import BODY_CACHE, listing_tag, response_tag

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
#: A text reader takes (bucket, key) and returns the body as UTF-8 text, never
#: parsed. Used only by `format: object` with `body: text` or `body: markdown`.
TextReader = Callable[[str, str], str]


def fetch(
    config: dict[str, Any],
    lister: StoreLister | None = None,
    reader: BodyReader | None = None,
    now: datetime | None = None,
    text_reader: TextReader | None = None,
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
    fmt = config.get("format", "json")
    if fmt == OBJECT_FORMAT and (config.get("records_path") or config.get("array_fields")):
        # A listing entry is ONE record. A fan-out over a body this format
        # never reads is a config typo, not something to guess around.
        return _failed(config, ("format",))
    body_mode = config.get("body")
    if body_mode is not None and (fmt != OBJECT_FORMAT or body_mode not in BODY_MODES):
        # `body` carries a document beside a listing record; on a parsed
        # format the body already IS the record, and an unknown mode is a typo.
        return _failed(config, ("body",))
    window_days = config.get("window_days")
    if window_days is not None and (isinstance(window_days, bool)
                                    or not isinstance(window_days, int)
                                    or window_days < 1):
        return _failed(config, ("window_days",))
    if lister is None or reader is None or (body_mode and text_reader is None):
        default_lister, default_reader, default_text = _default_s3()
        lister = lister or default_lister
        reader = reader or default_reader
        text_reader = text_reader or default_text
        missing = [n for n, v in (("lister", lister), ("reader", reader)) if v is None]
        if body_mode and text_reader is None:
            missing.append("text_reader")
        if missing:
            return _failed(config, tuple(missing))

    import re

    regex = re.compile(pattern)
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
        if fmt == OBJECT_FORMAT:
            # The listing entry IS the record: no body is fetched or parsed.
            body = _listing_record(key, last_modified, groups)
        else:
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

        key_entities: list[Entity] = []
        for record in records:
            mapped = _one_entity(
                record, body_root, groups, kind, key, bucket, source_label,
                last_modified, staleness_factor, cadence_seconds, now, config,
            )
            if mapped is None:
                partial = True
                continue
            if body_mode:
                mapped, readable = _with_document(
                    mapped, bucket, key, text_reader,  # type: ignore[arg-type]
                    int(config.get("body_max_bytes", DEFAULT_BODY_MAX_BYTES)),
                    body_mode)
                partial = partial or not readable
            if window_days is not None:
                mapped = replace(mapped, detail={**mapped.detail,
                                                 WINDOW_DETAIL: window_days})
            key_entities.append(mapped)
        entities.extend(key_entities)

        cid = groups.get("component_id")
        if cid:
            # The component produces the document (§3.3, §6) — declared even
            # when the fan-out below is empty.
            edges.append(Edge(source=cid, rel="produces", target=key))
            # alpha-engine-config-I8768: a grouped/list-of-dicts/parallel-array
            # body fans ONE key out into MANY entities whose ids are never
            # `key` itself — the edge above alone left every one of them with
            # zero inbound edges. Each record's own id is already this
            # adapter's own read (`mapped.id`, from the SAME record the entity
            # above was built from), so pointing at it invents no identifier
            # (§2.3). Skipped when the id already equals the key (the
            # whole-body, one-entity-per-key case) — that is the edge above.
            edges.extend(
                Edge(source=cid, rel="produces", target=e.id)
                for e in key_entities
                if e.id != key
            )

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


#: `format: object` (alpha-engine-config-I11816). The record is the LISTING
#: entry — the key, its last-modified stamp and the key pattern's named groups
#: — and the body is never read. This is for a source whose body is not a
#: record at all (a markdown report, a run log), where what a row can honestly
#: say is "this was published, for this date, at this time", and the reader
#: follows the evidence link for the content. Without it the default reader
#: `json.loads` such a body, fails, and the key is dropped as unreadable.
#:
#: `object-store` already lists without reading, but it mints an Artifact per
#: key with a fixed facet set and a cadence-staleness state, so every past
#: day's report would render `stale` forever. Here the kind, id, state and
#: facets stay config declarations like every other s3-records source, so a
#: dated series can be a Run per `job@date` and collapse to its newest row.
OBJECT_FORMAT = "object"


#: `body: text` (with `format: object` only). The listing entry is still the
#: record, and the object's text is carried beside it as `detail["document"]`
#: so the entity page shows the report itself: an `s3://` evidence link is not
#: something a browser can open. Read through the same ETag cache as every
#: other body, so an unchanged report is not re-downloaded on each rebuild.
BODY_TEXT = "text"
#: `body: markdown`: carried exactly as `body: text` is, with the document's
#: `format` declared as markdown so the entity page renders it (headings,
#: tables, links) instead of showing the raw text. The rendering — raw HTML
#: off, http(s)/relative links only — is the renderer's, not this adapter's.
BODY_MARKDOWN = DOCUMENT_MARKDOWN
BODY_MODES = (BODY_TEXT, BODY_MARKDOWN)
#: The largest document carried whole; a longer one is cut at this many bytes
#: and says so (`truncated`), never silently shortened. Override per source
#: with `body_max_bytes`.
DEFAULT_BODY_MAX_BYTES = 512 * 1024
#: `window_days` (`WINDOW_DETAIL`): the source declares that a list of its
#: rows opens on its last N days, newest first, with every row one link away.
#: Carried on each row, the same way `pane` and `question` are: the rows
#: declare their own dashboard, so no menu or pane registry is edited.


def _with_document(ent: Entity, bucket: str, key: str, text_reader: TextReader,
                   max_bytes: int, body_mode: str = BODY_TEXT) -> tuple[Entity, bool]:
    """`ent` with its object's text attached, and whether it could be read.

    An unreadable body keeps the row (the listing says the report exists) and
    records the failure as a named source finding, so the page says "could not
    read" rather than showing an empty document (§5.5).
    """
    try:
        text = text_reader(bucket, key)
    except Exception as exc:  # noqa: BLE001 - surfaced as a finding, not swallowed
        finding = {"condition": f"unreadable ({type(exc).__name__})"}
        return replace(ent, detail={**ent.detail, "document_source": finding}), False
    raw = text.encode("utf-8")
    truncated = len(raw) > max_bytes
    if truncated:
        text = raw[:max_bytes].decode("utf-8", errors="ignore")
    doc = {"text": text, "bytes": len(raw), "truncated": truncated, "format": body_mode}
    return replace(ent, detail={**ent.detail, DOCUMENT_DETAIL: doc}), True


def _listing_record(key: str, last_modified: str | None,
                    groups: dict[str, str]) -> dict[str, Any]:
    """The one record a `format: object` key carries. Named groups win over
    the two listing fields only if a pattern names a group `key` or
    `last_modified`, which is the pattern author's explicit choice."""
    return {"key": key, "last_modified": last_modified, **groups}


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
    if fmt == OBJECT_FORMAT:
        # A whole-body projection of the synthetic listing record.
        fmt = "json"
    return project(body, fmt, config.get("records_path"), config.get("array_fields"),
                    config.get("group_field"), config.get("limit"),
                    config.get("order"))


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
    # list. The grammar (path facets, spelled-out `{path:}`, and literal
    # `{value:}` facets that stamp a source's own identity on every record)
    # lives in `records_shape.resolve_facets`, shared with the driver (§2.3).
    facets = resolve_facets(config.get("facets"), path_root)

    return Entity(
        kind=kind,
        id=entity_id,
        state=state,
        provenance=Provenance(source=source_label, as_of=as_of, evidence=evidence),
        facets=facets,
        detail={"fields": fields_out, "key": key},
    )



def _default_s3() -> tuple[StoreLister | None, BodyReader | None, TextReader | None]:
    """boto3-backed lister, body reader and text reader when the optional AWS
    extra is installed."""
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ImportError:
        return None, None, None

    def lister(bucket: str, prefix: str) -> list[StoredObject]:
        client = _aws_client("s3")
        out: list[StoredObject] = []
        tags: dict[str, str | None] = {}
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
                tags[key] = listing_tag(obj)
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        # Only a COMPLETE listing reaches here (a failed page raises), so a key
        # it omits is gone and its cached body is dropped with it.
        BODY_CACHE.observe_listing(bucket, prefix, tags)
        return out

    def raw_bytes(bucket: str, key: str) -> bytes:
        client = _aws_client("s3")

        def get() -> tuple[bytes, str | None]:
            resp = client.get_object(Bucket=bucket, Key=key)
            return resp["Body"].read(), response_tag(resp)

        try:
            # An unchanged ETag since the last listing reuses the last body.
            return BODY_CACHE.read(bucket, key, get)
        except (BotoCoreError, ClientError):
            raise

    def reader(bucket: str, key: str) -> Any:
        raw = raw_bytes(bucket, key)
        if key.endswith(".csv"):
            return raw.decode("utf-8")
        return json.loads(raw.decode("utf-8"))

    def text_reader(bucket: str, key: str) -> str:
        return raw_bytes(bucket, key).decode("utf-8")

    return lister, reader, text_reader
