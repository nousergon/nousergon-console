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

Lineage (§3.3/§6, `nousergon-console#52`): a ``component_id`` named group
derives the ``produces`` edge (the key's own producer). A key pattern may
symmetrically name a ``consumer_id`` group — the id of whichever component the
deploying operator has configured as this key's reader — which derives the
``consumed-by`` edge the same way. Both are config-declared identifiers, never
inferred: this adapter has no way to discover who reads an object-store key on
its own, only what the key pattern's own named groups say (§2.3).

**Two access modes, one source shape** (`docs/adapters.md`'s boundary test,
step 3 — fold the capability in rather than forking the adapter). The default
mode LISTS a prefix and matches keys against ``key_pattern``. The second mode
(``keys:``) HEADs a list of DECLARED keys, one call each, and never lists
anything: it is the OBSERVATION half of a `declared-registry`
(alpha-engine-config-I8765), pointed at the same identifiers so the index
merges declaration and observation into one row (§2.5) and a real read decides
`fresh`/`stale`/`absent`.

- **A prefix listing was not an option here.** The registry's 183 keys sit
  under dozens of dated prefixes; enumerating them is the same cost class as
  the 74.5s `pipeline-reliability` fetch (alpha-engine-config-I7424) against a
  180s refresh budget. A HEAD per key is bounded, parallel and cheap.
- **The entity id is the key AS DECLARED — the template, not the resolved
  key.** §3.2: the identifier is the URL, so an id carrying today's date would
  break every saved link nightly and would never merge with the declaration
  claim, which is keyed by the template. The resolved key is the EVIDENCE.
- **A key that cannot be honestly resolved is not looked at.** No claim is
  emitted, the row stays declared-and-`unobserved`, and it shows up in
  `index/numbers.py::artifact_observation_coverage` as the coverage gap it is.
  HEADing a key the fleet never writes would render its 404 as a finding, which
  is the defect this whole slice exists to remove.

**`keys_from:` — the key list is BOUND to the registry, never copied into
config.** The literal `keys:` list stays supported for a deployment with a
handful of them, but a registry of any size is bound: `keys_from` names the
same document, entries field and id field the `declared-registry` fragment
already names, and the key list, each key's cadence and its partition resolver
are derived at FETCH time. Three reasons, in the order they bind:

1. **A generated copy is a drift source.** The first cut of this generated a
   170-key fragment from the registry with a `--check` job to prove it still
   matched. That check is a detector for a divergence that binding cannot have:
   every generated line is a line that can be stale, and the console holds the
   registry path already.
2. **The copy did not fit.** Measured (alpha-engine-config-I8765): the
   generated fragment was 25,799 bytes and this deployment's whole assembled
   config is ONE SSM parameter capped at 4,096 characters. The bound fragment
   is ~15 lines.
3. **Declaration and observation must agree about the document.** Both halves
   walk it through `console/registry_document.py` — see that module for why one
   copy of the walk is a correctness property here, not a tidiness one.

The overrides that genuinely are per-deployment judgements stay declared and
stay small: `partition_by_cadence` (which partition a cadence's run writes) and
`partition_overrides` (the individual keys whose producer disagrees with that
rule). Both are measurements the config records; neither is inferable, and
together they are a dozen lines rather than one line per key.

A declared ``question`` (`console-policy.md` §4.4, `nousergon-console#61`) is
carried as a synthetic ``text`` declared field, exactly matching the
``s3-records`` adapter's own convention — the two are the right shape to
share this one small piece: a source whose declared question needs no body
content at all (a markdown briefing, a parquet dump — this adapter never
reads a body, which is the whole reason to reach for it over ``s3-records``)
still gets its question rendered by the existing declared-fields table, with
no new rendering code either adapter.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .. import calendar_cadence
from .. import registry_document
from ..freshness import (
    ABSENT as _ABSENT,
    DISABLED as _DISABLED,
    NO_STAMP as _NO_STAMP,
    NOT_EXPECTED as _NOT_EXPECTED,
    freshness as _freshness,
)
from ..model.entity import Entity, Provenance
from ..index.build import now_iso
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass
from ..model.kinds import Kind
from ..aws import client as _aws_client
from ..drivers.context import default_object_stat as _default_stat
from ..trading_calendar import TradingDayChecker, default_trading_day_checker

#: Object listings are an OBSERVATION (§2.5) — what is there and how fresh.
CLAIM_CLASS = ClaimClass.OBSERVATION

name = "checks"
produces = ("component", "run", "artifact")

#: One stored object: its key and an optional ISO last-modified stamp.
StoredObject = tuple[str, str | None]
#: A lister takes (bucket, prefix) and returns the objects under it. Injectable.
StoreLister = Callable[[str, str], list[StoredObject]]
#: A stat takes one `s3://bucket/key` URI and returns its last-modified stamp,
#: or `None` when the object is not there. Injectable, and shared verbatim with
#: `drivers/object_store.py` (`drivers/context.py::default_object_stat`) — one
#: HEAD implementation, not a second one that could disagree about a 404.
KeyStat = Callable[[str], "str | None"]


def fetch(
    config: dict[str, Any],
    lister: StoreLister | None = None,
    now: datetime | None = None,
    stat: KeyStat | None = None,
    trading_day_checker: TradingDayChecker | None = None,
) -> AdapterResult:
    if config.get("keys") is not None or config.get("keys_from") is not None:
        return _fetch_declared_keys(config, stat, now, trading_day_checker)
    bucket = config.get("bucket")
    prefix = config.get("prefix", "")
    pattern = config.get("key_pattern")
    if not bucket or not pattern:
        return AdapterResult(
            claim_class=CLAIM_CLASS,
            fetched_at=now_iso(),
            name=config.get("_name", name),
            status=AdapterStatus.FAILED,
            unavailable=("all",),
        )
    if lister is None:
        lister = _default_lister()
        if lister is None:
            # boto3 not installed — declare unable rather than silently zero (§5.5).
            return AdapterResult(
                claim_class=CLAIM_CLASS,
                fetched_at=now_iso(),
                name=config.get("_name", name),
                status=AdapterStatus.FAILED,
                unavailable=("lister",),
            )

    regex = re.compile(pattern)
    cadence = _parse_cadence(config.get("cadence"))
    staleness_factor = float(config.get("staleness_factor", 1.5))
    question = config.get("question")
    now = now or datetime.now(timezone.utc)

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
    edges = []
    for key, last_modified in objects:
        m = regex.match(key)
        if not m:
            continue
        groups = m.groupdict()
        state = _state(last_modified, cadence, staleness_factor, now)
        detail: dict[str, Any] = {k: v for k, v in groups.items()}
        if cadence is not None:
            # §9.6 (alpha-engine-config-I7050): this adapter already computes
            # its OWN staleness verdict into `state` above. Also exposing the
            # cadence as a plain minute count lets `staleness_honesty()`
            # independently RE-DERIVE that verdict from `as_of` rather than
            # trusting it — the entire point of an honesty check, and
            # previously unreachable for every object-store-sourced artifact.
            detail["cadence_minutes"] = cadence / 60.0
        if question:
            # Synthetic declared field (§5.8) — rendered by the existing
            # declared-fields table, matching `s3-records`'s own convention
            # for the same config key (§4.4).
            detail["fields"] = {"question": {"value": str(question), "render": "text"}}
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
            detail=detail,
        )
        entities.append(art)
        # If the pattern names a component_id, derive the produces-edge from
        # the registry-side id so the artifact joins to its component (§3.3).
        cid = groups.get("component_id")
        if cid:
            from ..model.entity import Edge

            edges.append(Edge(source=cid, rel="produces", target=key))
        # Symmetrically, a consumer_id group derives the consumed-by edge
        # (§3.3/§6, nousergon-console#52). Declared lineage only: this
        # adapter reads one S3-compatible prefix and nothing else, so a
        # consumer can only be named by the deploying operator's own key
        # pattern, never discovered by reaching into another adapter (§2.3).
        consumer_id = groups.get("consumer_id")
        if consumer_id:
            from ..model.entity import Edge

            edges.append(Edge(source=key, rel="consumed-by", target=consumer_id))

    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        declared_cadence_seconds=cadence,
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
    """The shared verdict (`console/freshness.py`), with THIS reader's own
    "missing" token: a LISTING returned the key, so the object exists and could
    not be dated — `no-freshness-stamp`, which is a different finding from the
    keys mode's `absent` (we HEADed and it is not there). Staleness is rendered,
    never inferred by the reader (§5.2).
    """
    return _freshness(last_modified, cadence_seconds, staleness_factor, now,
                      missing=_NO_STAMP)


def _default_lister() -> StoreLister | None:
    """boto3-backed lister when the optional AWS extra is installed."""
    try:
        import boto3  # type: ignore
    except ImportError:
        return None

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


# ------------------------------------------------------------ keys mode ----

def _fetch_declared_keys(
    config: dict[str, Any],
    stat: KeyStat | None,
    now: datetime | None,
    trading_day_checker: TradingDayChecker | None,
) -> AdapterResult:
    """HEAD each declared key — the OBSERVATION half of a declared registry.

    Never lists. Every entity carries the key AS DECLARED as its id, so the
    claim merges with the declaration keyed the same way (§2.5), and carries
    the resolved key as its evidence.
    """
    binding = config.get("keys_from")
    entries: list[Any] = list(config.get("keys") or [])
    bucket = config.get("bucket")
    if binding is not None:
        try:
            bound, bound_bucket = _entries_from_registry(binding)
        except Exception:
            # The registry document could not be read. Every declared row then
            # stays `unobserved` — a counted coverage gap — rather than the
            # adapter emitting a partial reading of a document it did not get
            # through (§5.5).
            return _failed(config, ("keys_from",))
        entries.extend(bound)
        bucket = bucket or bound_bucket
    if not bucket or not entries:
        return _failed(config, ("all",))
    if stat is None:
        stat = _default_stat()
        if stat is None:
            # boto3 not installed — declare unable rather than silently zero
            # (§5.5). Every declared row then stays `unobserved`, which is the
            # honest answer and is counted as a coverage gap.
            return _failed(config, ("stat",))

    now = now or datetime.now(timezone.utc)
    # Resolved ONCE. `default_trading_day_checker()` builds a calendar and its
    # own cache per call, and this loop runs it per key — 170 of them on the
    # fleet's registry, which is a real cost inside a 180s refresh budget.
    checker = trading_day_checker or default_trading_day_checker()
    default_factor = float(config.get("staleness_factor", 1.5))
    default_resolver = str(config.get("partition", calendar_cadence.RUN_DATE))
    date_format = str(config.get("date_format", calendar_cadence.DEFAULT_DATE_FORMAT))
    by_cadence = {str(k): str(v) for k, v in
                  (config.get("partition_by_cadence") or {}).items()}
    overrides = {str(k): str(v) for k, v in
                 (config.get("partition_overrides") or {}).items()}

    resolved: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    entities: list[Entity] = []
    unresolved = 0
    not_an_object = 0
    not_expected = 0
    disabled = 0
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("key"):
            unresolved += 1
            continue
        template = str(entry["key"])
        if template.endswith("/"):
            # A prefix, not an object: HEAD is always a 404, so looking would
            # manufacture an absence out of a key shape. Named separately in
            # `unavailable` because the fix is a registry edit, not a resolver.
            not_an_object += 1
            continue
        # `alpha-engine-config-I8780`: a row that DECLARES it will not be
        # written per-cycle (`cadence: event_driven`) or declares itself off
        # (`lifecycle: disabled`) is a decision the row's own registry entry
        # already states — HEADing it and reading a miss as `absent` renders
        # a declared decision as a defect (§8.3's DISABLED-vs-MISSED pair,
        # extended to Artifact values). Neither is looked at: no HEAD is
        # issued, the row still renders (never narrows the denominator,
        # `alpha-engine-config-I8780`'s constraint), and its state is its
        # OWN declaration rather than a substrate read.
        lifecycle = str(entry.get("lifecycle") or "").strip().lower()
        entry_cadence = str(entry.get("cadence") or "").strip().lower()
        if lifecycle == "disabled":
            disabled += 1
            entities.append(_declared_entity(entry, template, bucket, _DISABLED))
            continue
        if entry_cadence == "event_driven":
            not_expected += 1
            entities.append(_declared_entity(entry, template, bucket, _NOT_EXPECTED))
            continue
        detail: dict[str, Any] = {}
        calendar_cadence.apply_declared_cadence(
            detail, entry, now=now, trading_day_checker=checker)
        key = calendar_cadence.resolve_key_template(
            template,
            cadence=entry.get("cadence"),
            resolver=_resolver_for(entry, template, by_cadence, overrides,
                                   default_resolver),
            now=now,
            trading_day_checker=checker,
            date_format=str(entry.get("date_format", date_format)),
        )
        if key is None:
            # Declared, and not honestly resolvable to a partition. Emitting
            # nothing leaves the declaration's `unobserved` standing.
            unresolved += 1
            continue
        resolved.append((entry, key, detail))

    stamps, unreadable = _stat_all(stat, bucket, [k for _, k, _ in resolved],
                                   int(config.get("max_workers", 8)))

    for entry, key, detail in resolved:
        if key in unreadable:
            # The source could not be read for this key. Skipping is what keeps
            # a transient error from rendering as a fleet finding (§2.3); the
            # adapter says so in `unavailable` instead.
            continue
        cadence_minutes = detail.get("cadence_minutes")
        cadence_seconds = float(cadence_minutes) * 60.0 if cadence_minutes else None
        last_modified = stamps.get(key)
        detail["resolved_key"] = key
        if entry.get("cadence"):
            detail["declared_cadence"] = entry["cadence"]
        entities.append(Entity(
            kind=Kind.ARTIFACT,
            id=str(entry["key"]),  # the DECLARED key — the merge identifier (§3.2)
            state=_freshness(
                last_modified, cadence_seconds,
                float(entry.get("staleness_factor", default_factor)),
                now, missing=_ABSENT,
            ),
            provenance=Provenance(
                source=f"s3://{bucket}/{key}",
                as_of=last_modified,
                evidence=f"s3://{bucket}/{key}",
            ),
            facets={"repo": bucket},
            detail=detail,
        ))

    unavailable: list[str] = []
    if unresolved:
        unavailable.append(f"unresolved-partition:{unresolved}")
    if not_an_object:
        unavailable.append(f"not-an-object:{not_an_object}")
    if not_expected:
        unavailable.append(f"not-expected:{not_expected}")
    if disabled:
        unavailable.append(f"disabled:{disabled}")
    if unreadable:
        unavailable.append(f"unreadable-keys:{len(unreadable)}")
    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        name=config.get("_name", name),
        status=AdapterStatus.OK,
        entities=tuple(entities),
        unavailable=tuple(unavailable),
    )


def _declared_entity(
    entry: dict[str, Any], template: str, bucket: str | None, state: str,
) -> Entity:
    """A `keys_from` row rendered from its OWN declaration, never HEADed.

    `alpha-engine-config-I8780`: used for `lifecycle: disabled` and
    `cadence: event_driven` rows. `as_of=None` is a declared absence of a
    freshness stamp (`model/entity.py::Provenance`), not a silent default —
    this row was never read, on purpose, because its own registry entry
    already answers the question a HEAD would have asked.
    """
    detail: dict[str, Any] = {"resolved_key": template}
    if entry.get("cadence"):
        detail["declared_cadence"] = entry["cadence"]
    if entry.get("lifecycle"):
        detail["declared_lifecycle"] = entry["lifecycle"]
    return Entity(
        kind=Kind.ARTIFACT,
        id=str(entry["key"]),  # the DECLARED key — the merge identifier (§3.2)
        state=state,
        provenance=Provenance(
            source=f"s3://{bucket}/{template}" if bucket else template,
            as_of=None,
            evidence=None,
        ),
        facets={"repo": bucket} if bucket else {},
        detail=detail,
    )


def _failed(config: dict[str, Any], unavailable: tuple[str, ...]) -> AdapterResult:
    """A source this adapter could not read at all — one FAILED envelope naming
    the cause.

    Was NEVER DEFINED when the keys mode landed (`nousergon-console-PR112`):
    all three of its unreadable-source paths called this name and raised
    `NameError` instead. Live only where boto3 is absent or the fragment is
    malformed, so nothing on the box hit it — which is the point. An adapter's
    honest "I could not read this" path is exactly the path no ordinary run
    exercises, so it is the one that must be tested rather than assumed
    (§5.5: unable is a declared status, never an exception escaping the
    adapter).
    """
    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        name=config.get("_name", name),
        status=AdapterStatus.FAILED,
        unavailable=unavailable,
    )


def _resolver_for(
    entry: dict[str, Any],
    key: str,
    by_cadence: dict[str, str],
    overrides: dict[str, str],
    default_resolver: str,
) -> str:
    """Which partition the last expected run of this key's cadence WROTE.

    Three declared layers, most specific first — the entry's own `partition`,
    a per-key override, then the cadence's rule. All three are CONFIG (§2.3):
    a run's date and the partition it writes are different facts and the second
    is not derivable from the first, so this module never infers one from the
    other. An override exists because a real registry disagrees with its own
    cadence rule on individual rows; expressing that as three small declared
    tables is what keeps a hundred-row registry from being transcribed key by
    key into config.
    """
    declared = entry.get("partition")
    if declared:
        return str(declared)
    if key in overrides:
        return overrides[key]
    cadence = entry.get("cadence")
    if cadence and str(cadence) in by_cadence:
        return by_cadence[str(cadence)]
    return default_resolver


def _entries_from_registry(binding: Any) -> tuple[list[dict[str, Any]], str | None]:
    """`keys:` entries derived from the SAME document the declaration half
    reads (`console/registry_document.py`), plus the bucket it declares.

    The alternative — generating a `keys:` list into config from that document
    — was built first and rejected: it is a second copy of a hundred-odd keys
    that drifts the moment the registry changes, it cannot be re-derived by the
    console at read time, and (measured, alpha-engine-config-I8765) the
    generated fragment was 25,799 bytes against a config parameter store whose
    whole body must fit in 4,096. Binding reads one document at fetch time and
    holds nothing to drift.

    The registry's own row IS the entry: this only renames the two fields the
    caller's config names (`id_field` -> `key`, `cadence_field` -> `cadence`),
    so every other declared field a row carries — `sla_minutes_after_cron`,
    `interval_minutes`, an explicit `partition` — reaches
    `calendar_cadence.apply_declared_cadence` under the name it already has.
    """
    if not isinstance(binding, Mapping):
        raise ValueError("keys_from must be a mapping")
    path = binding.get("path")
    if not path:
        raise ValueError("keys_from declares no `path`")
    id_field = str(binding.get("id_field", "id"))
    cadence_field = str(binding.get("cadence_field", "cadence"))
    document = registry_document.load(str(path))
    bucket = None
    bucket_from = binding.get("bucket_from")
    if bucket_from:
        # The registry declares its own bucket; repeating it in console config
        # is one more literal that can drift away from the document the keys
        # come from.
        candidate = registry_document.dig(document, str(bucket_from))
        bucket = str(candidate) if isinstance(candidate, str) else None
    raw = registry_document.dig(document, binding.get("entries_field"))
    out: list[dict[str, Any]] = []
    for mapping_key, entry in registry_document.entries(raw):
        eid = registry_document.entry_id(entry, mapping_key, id_field)
        if not eid:
            continue
        derived = dict(entry)
        derived["key"] = eid
        if cadence_field != "cadence":
            derived["cadence"] = entry.get(cadence_field)
        out.append(derived)
    return out, bucket


def _stat_all(
    stat: KeyStat, bucket: str, keys: list[str], max_workers: int,
) -> tuple[dict[str, str | None], set[str]]:
    """`(key -> stamp-or-None, keys the source could not be read for)`.

    Parallel because the whole point of HEAD-per-key over a prefix listing is a
    bounded, cheap fetch: 183 sequential round trips would put this adapter in
    the cost class it was chosen to avoid. `None` is a RESULT (the object is not
    there); an exception is the source failing, which is a different fact and is
    reported as such rather than rendered as absence.
    """
    stamps: dict[str, str | None] = {}
    unreadable: set[str] = set()

    def one(key: str) -> tuple[str, str | None, bool]:
        try:
            return key, stat(f"s3://{bucket}/{key}"), True
        except Exception:  # noqa: BLE001 - a state, never an exception (§2.3)
            return key, None, False

    if max_workers > 1 and len(keys) > 1:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(keys))) as pool:
            results = list(pool.map(one, keys))
    else:
        results = [one(k) for k in keys]
    for key, stamp, ok in results:
        if ok:
            stamps[key] = stamp
        else:
            unreadable.add(key)
    return stamps, unreadable
