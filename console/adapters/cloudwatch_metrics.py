"""cloudwatch-metrics adapter — Component state from a metric namespace.

Reads one CloudWatch metric namespace, enumerating the substrate by the
dimension values the namespace itself carries, and maps each one's invocation
and error counts over a declared trailing window onto a Component entity with
the §5.1 four-field row contract (state · source · as-of · evidence).

Generic by construction (§2.3): the namespace, the dimension, the two metric
names, the window and the region all come from configuration. Nothing here
knows about Lambda, or about any fleet — pointing it at `AWS/Lambda` /
`FunctionName` is a config entry, and pointing a second instance at
`AWS/States` / `StateMachineArn` would be another.

## Why the identifier merges rather than double-rendering

The dimension value **is** the component id, verbatim, never slug-minted
(§3.6). The fleet registry declares Lambda components under their deployed
function names, so the declaration and this observation merge under §2.5
instead of rendering the same component twice.

## Why this is a DISCOVERY claim and not an OBSERVATION

The adapter's primary statement is *the substrate has this thing* — it learns
what exists by enumerating the namespace, not by anything reporting in. Its
state readings are derived from the substrate's own counters rather than from
the component saying what it did, so they must lose to a real emitted
envelope when one exists (`_state_rank`: observation 1, discovery 2,
declaration-without-lifecycle 3). They still beat a bare declaration, which is
what turns a registered-but-silent row into a rendered state.

Declaring DISCOVERY is also the ONLY thing that makes `ABSENT` computable for
this substrate: `index/graph.py::_reconcile` renders a declared component
`ABSENT` when a discovery pass ran fine and did not find it. A registry row
naming something the metric namespace has never heard of is exactly that
finding — and it is a finding, not a defect to be papered over.

`discovery_scope` on the result is what keeps that honest. This pass
enumerates ONE substrate; it has no opinion about the fleet's GitHub Actions
workflows or laptop launchd agents, and without the scope the index would
assert `ABSENT` over all of them (see `model/envelope.py`).

## State mapping — and why zero is never green

Over the configured trailing window, per `observability-policy.md` §8.3:

| Condition | State |
|---|---|
| `errors > 0` | `FAILED` |
| `invocations > 0`, `errors == 0` | `HEALTHY` |
| `invocations == 0`, and no datapoint in the history lookback | `NEVER_RAN` |
| `invocations == 0`, with a datapoint in the history lookback | `UNREPORTED` |
| declared in the registry, absent from the namespace | `ABSENT` (by merge) |

The fourth row is the one that costs something and it is deliberate. A
component that has invoked before but not inside the window is either idle by
design or missed its trigger — `DISABLED` vs `MISSED`, the pair §8.3 exists to
keep apart — and **metrics alone cannot tell them apart.** Only a declared
cadence can, and an adapter may not read the registry that would carry one
(§2.3). §8.3's answer where the classifier genuinely cannot place a component
is `UNREPORTED`: loud, and a finding. The row still carries its last
invocation and its counts in `detail`, so the reader is not left guessing —
but it is not rendered green, and it stays counted in the transparency gap
until the registry declares what its cadence should be.

## Cost, and why the adapter caches

`GetMetricData` is billed per metric requested. At the console's 60-second
index rebuild an uncached pass over N components x 2 metrics is 2880N metrics
a day; the same read on a 900-second cadence is 192N. So the adapter holds its
own TTL cache keyed by configured name (`cost-management-policy.md`), serves
the cached result inside `cadence_seconds`, and declares that cadence on the
result so §5.9's freshness bound reflects what was actually read rather than
when the index happened to rebuild.

Hermetic: enumeration and metric reading are two injectable callables, so
tests run over recorded fixtures with no live CloudWatch (groom-sweep §8.1).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..model.entity import Entity, Provenance
from ..index.build import now_iso
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass
from ..model.kinds import Kind, State

#: Enumerating a metric namespace establishes that a thing is THERE — §2.5's
#: DISCOVERY. See the module docstring for why this is not OBSERVATION.
CLAIM_CLASS = ClaimClass.DISCOVERY

name = "cloudwatch-metrics"
produces = ("component",)

#: (namespace, metric_name, dimension_name) -> the dimension values present.
Enumerator = Callable[[str, str, str], list[str]]

@dataclass(frozen=True)
class MetricQuery:
    """One metric to read. Self-contained on purpose: the reader is injectable,
    so a query that leans on the caller's closure for its namespace or its
    dimension name cannot be satisfied by a test fixture or a second backend."""

    query_id: str
    namespace: str
    dimension_name: str
    dimension_value: str
    metric_name: str
    period_seconds: int


#: (queries, start, end) -> query_id -> [(iso_timestamp, value), …]. A query
#: with no datapoints returns an empty list, never a zero — the difference
#: between "ran zero times" and "the namespace has never heard of it" is the
#: whole NEVER_RAN/HEALTHY distinction.
MetricReader = Callable[
    [list[MetricQuery], datetime, datetime],
    dict[str, list[tuple[str, float]]],
]

_DEFAULT_NAMESPACE = "AWS/Lambda"
_DEFAULT_DIMENSION = "FunctionName"
_DEFAULT_INVOCATIONS = "Invocations"
_DEFAULT_ERRORS = "Errors"
_DEFAULT_WINDOW_MINUTES = 1440
_DEFAULT_HISTORY_DAYS = 14
_DEFAULT_CADENCE_SECONDS = 900

#: name → (read_at_epoch_seconds, result). Module-level and deliberately so:
#: the console rebuilds its index by calling `fetch` again, so an instance the
#: adapter could hang state off does not exist. Keyed by the adapter's
#: CONFIGURED name, so two instances pointed at different namespaces never
#: share an entry. `_clock` is injectable for tests.
_CACHE: dict[str, tuple[float, AdapterResult]] = {}


def reset_cache() -> None:
    """Drop the TTL cache. For tests and for `doctor`'s forced re-read."""
    _CACHE.clear()


def fetch(
    config: dict[str, Any],
    enumerator: Enumerator | None = None,
    reader: MetricReader | None = None,
    now: datetime | None = None,
    clock: Callable[[], float] | None = None,
) -> AdapterResult:
    configured_name = config.get("_name", name)
    namespace = config.get("namespace") or _DEFAULT_NAMESPACE
    dimension = config.get("dimension") or _DEFAULT_DIMENSION
    invocations_metric = config.get("invocations_metric") or _DEFAULT_INVOCATIONS
    errors_metric = config.get("errors_metric") or _DEFAULT_ERRORS
    window_minutes = int(config.get("window_minutes") or _DEFAULT_WINDOW_MINUTES)
    history_days = int(config.get("history_days") or _DEFAULT_HISTORY_DAYS)
    cadence_seconds = float(config.get("cadence_seconds") or _DEFAULT_CADENCE_SECONDS)
    region = config.get("region")
    id_pattern = config.get("id_pattern")
    scope = _discovery_scope(config)

    clock = clock or _monotonic
    cached = _CACHE.get(configured_name)
    if cached is not None and (clock() - cached[0]) < cadence_seconds:
        return cached[1]

    if enumerator is None or reader is None:
        default_enumerator, default_reader = _default_cloudwatch(region)
        enumerator = enumerator or default_enumerator
        reader = reader or default_reader
        missing = [
            label for label, fn in (("enumerator", enumerator), ("reader", reader))
            if fn is None
        ]
        if missing:
            # boto3 absent → FAILED and named, never zero rows silently (§5.5).
            return AdapterResult(
                claim_class=CLAIM_CLASS,
                fetched_at=now_iso(),
                name=configured_name,
                status=AdapterStatus.FAILED,
                unavailable=tuple(missing),
                discovery_scope=scope,
            )

    now = now or datetime.now(timezone.utc)
    source_label = f"cloudwatch:{namespace}"

    try:
        ids = enumerator(namespace, invocations_metric, dimension)
    except Exception:
        # A discovery pass that could not enumerate has NOT established
        # absence. FAILED with no entities, so `_reconcile`'s ABSENT guard —
        # which requires a *successful* discovery — correctly does not fire.
        return AdapterResult(
            claim_class=CLAIM_CLASS,
            fetched_at=now_iso(),
            name=configured_name,
            status=AdapterStatus.FAILED,
            unavailable=("source",),
            discovery_scope=scope,
        )

    if id_pattern:
        regex = re.compile(id_pattern)
        ids = [i for i in ids if regex.search(i)]
    ids = sorted(set(ids))

    window_start = now - timedelta(minutes=window_minutes)
    window_seconds = max(60, window_minutes * 60)
    queries: list[MetricQuery] = []
    for ordinal, entity_id in enumerate(ids):
        for suffix, metric in (("i", invocations_metric), ("e", errors_metric)):
            queries.append(MetricQuery(
                query_id=f"w{ordinal}{suffix}",
                namespace=namespace,
                dimension_name=dimension,
                dimension_value=entity_id,
                metric_name=metric,
                period_seconds=window_seconds,
            ))

    try:
        window_data = reader(queries, window_start, now) if queries else {}
    except Exception:
        return AdapterResult(
            claim_class=CLAIM_CLASS,
            fetched_at=now_iso(),
            name=configured_name,
            status=AdapterStatus.FAILED,
            unavailable=("source",),
            discovery_scope=scope,
        )

    # Only the silent ones need the expensive history read — the whole point
    # of the second pass is telling NEVER_RAN from idle, and anything that
    # invoked inside the window has already answered that question.
    silent = [
        (ordinal, entity_id) for ordinal, entity_id in enumerate(ids)
        if _total(window_data.get(f"w{ordinal}i")) == 0.0
    ]
    history_start = now - timedelta(days=history_days)
    history_queries: list[MetricQuery] = [
        MetricQuery(
            query_id=f"h{ordinal}i",
            namespace=namespace,
            dimension_name=dimension,
            dimension_value=entity_id,
            metric_name=invocations_metric,
            period_seconds=86400,
        )
        for ordinal, entity_id in silent
    ]
    partial = False
    history_data: dict[str, list[tuple[str, float]]] = {}
    if history_queries:
        try:
            history_data = reader(history_queries, history_start, now)
        except Exception:
            # The window read succeeded; only the NEVER_RAN discriminator is
            # missing. Declare it and classify the silent ones UNREPORTED
            # rather than guessing NEVER_RAN on a read that did not happen.
            partial = True

    entities: list[Entity] = []
    for ordinal, entity_id in enumerate(ids):
        invocations = _total(window_data.get(f"w{ordinal}i"))
        errors = _total(window_data.get(f"w{ordinal}e"))
        history = history_data.get(f"h{ordinal}i") or []
        last_invocation = _last_nonzero(history)
        state, reason = _state(
            invocations=invocations,
            errors=errors,
            has_history_read=(f"h{ordinal}i" in history_data),
            last_invocation=last_invocation,
        )
        as_of = _latest_stamp(window_data.get(f"w{ordinal}i")) or last_invocation
        detail: dict[str, Any] = {
            "invocations": invocations,
            "errors": errors,
            "window_minutes": window_minutes,
            "namespace": namespace,
            "dimension": dimension,
        }
        if last_invocation:
            detail["last_invocation"] = last_invocation
        if reason:
            detail["unplaceable_reason"] = reason
        entities.append(Entity(
            kind=Kind.COMPONENT,
            id=entity_id,  # the dimension value verbatim (§3.6)
            state=state,
            provenance=Provenance(
                source=source_label,
                as_of=as_of or now.isoformat(),
                evidence=_evidence(region, namespace, dimension, entity_id),
            ),
            detail=detail,
        ))

    result = AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        declared_cadence_seconds=cadence_seconds,
        name=configured_name,
        status=AdapterStatus.OK,
        entities=tuple(entities),
        # §2.3: what this source cannot supply, named rather than guessed. It
        # reads counters, so it has nothing to say about cost or cadence, and
        # `cadence` is the load-bearing one — its absence is exactly why a
        # silent component renders UNREPORTED rather than MISSED.
        unavailable=("cadence", "cost", "history") if partial
        else ("cadence", "cost"),
        discovery_scope=scope,
    )
    _CACHE[configured_name] = (clock(), result)
    return result


def _discovery_scope(config: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """The (facet, value) slice this pass claims to have enumerated.

    Configured, because which facet identifies "the substrate this namespace
    covers" is a fact about the deployment's registry, not about CloudWatch.
    Declaring nothing claims the whole fleet, which for a single namespace is
    almost never true — so a deployment that omits it gets the conservative
    reading only because the index treats an unscoped pass as fleet-wide, and
    the shipped config always sets it.
    """
    facet = config.get("discovery_facet")
    value = config.get("discovery_value")
    if facet and value:
        return ((str(facet), str(value)),)
    return ()


def _state(
    invocations: float,
    errors: float,
    has_history_read: bool,
    last_invocation: str | None,
) -> tuple[State, str | None]:
    """The mapping in the module docstring. `HEALTHY` never answers "no data"."""
    if errors > 0:
        return State.FAILED, None
    if invocations > 0:
        return State.HEALTHY, None
    if has_history_read and last_invocation is None:
        # Enumerated by the namespace, and no invocation anywhere in the
        # lookback: registered and in service with no run in its history.
        return State.NEVER_RAN, None
    if not has_history_read:
        return State.UNREPORTED, (
            "zero invocations in the window and the history lookback could not "
            "be read — NEVER_RAN is not established, so it is not claimed"
        )
    return State.UNREPORTED, (
        "zero invocations in the window, but it has invoked before. Idle by "
        "design and missed-its-trigger are indistinguishable from metrics "
        "alone; only a declared cadence separates DISABLED from MISSED "
        "(observability-policy.md §8.3), and this row declares none"
    )


def _total(points: list[tuple[str, float]] | None) -> float:
    return float(sum(v for _, v in points or []))


def _latest_stamp(points: list[tuple[str, float]] | None) -> str | None:
    stamps = [t for t, _ in points or [] if t]
    return max(stamps) if stamps else None


def _last_nonzero(points: list[tuple[str, float]]) -> str | None:
    stamps = [t for t, v in points if v and t]
    return max(stamps) if stamps else None


def _evidence(
    region: str | None, namespace: str, dimension: str, entity_id: str
) -> str:
    """A console deep link when the region is configured, else the metric id.

    Never None-by-default: §5.1 wants a "go look" for every row, and the
    metric's own coordinates are the honest fallback when no region was given.
    """
    if not region:
        return f"{namespace}/{dimension}={entity_id}"
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#metricsV2:graph=~();query=~'{namespace}"
        f"*3a{dimension}*3d{entity_id}"
    )


def _monotonic() -> float:
    import time

    return time.monotonic()


def _default_cloudwatch(
    region: str | None,
) -> tuple[Enumerator | None, MetricReader | None]:
    """boto3-backed enumerator + metric reader when the AWS extra is installed.

    Returns ``(None, None)`` when boto3 is absent so the adapter fails loud
    rather than silently returning zero rows (§5.5) — and, critically, so the
    absent pass never satisfies `_reconcile`'s successful-discovery guard and
    paints a substrate `ABSENT` on a missing dependency.
    """
    try:
        import boto3  # type: ignore
    except ImportError:
        return None, None

    def _client():
        return boto3.client("cloudwatch", region_name=region) if region \
            else boto3.client("cloudwatch")

    def enumerator(namespace: str, metric: str, dimension: str) -> list[str]:
        client = _client()
        out: list[str] = []
        paginator = client.get_paginator("list_metrics")
        for page in paginator.paginate(Namespace=namespace, MetricName=metric,
                                       Dimensions=[{"Name": dimension}]):
            for entry in page.get("Metrics") or []:
                for dim in entry.get("Dimensions") or []:
                    if dim.get("Name") == dimension and dim.get("Value"):
                        out.append(dim["Value"])
        return out

    def reader(
        queries: list[MetricQuery], start: datetime, end: datetime,
    ) -> dict[str, list[tuple[str, float]]]:
        client = _client()
        out: dict[str, list[tuple[str, float]]] = {}
        # GetMetricData accepts 500 queries per call; batching is what keeps
        # the pass one-or-two API calls rather than one per component.
        for batch in (queries[i:i + 500] for i in range(0, len(queries), 500)):
            spec = [
                {
                    "Id": query.query_id,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": query.namespace,
                            "MetricName": query.metric_name,
                            "Dimensions": [{
                                "Name": query.dimension_name,
                                "Value": query.dimension_value,
                            }],
                        },
                        "Period": query.period_seconds,
                        "Stat": "Sum",
                    },
                    "ReturnData": True,
                }
                for query in batch
            ]
            response = client.get_metric_data(
                MetricDataQueries=spec, StartTime=start, EndTime=end,
            )
            for entry in response.get("MetricDataResults") or []:
                out[entry["Id"]] = [
                    (t.isoformat() if hasattr(t, "isoformat") else str(t), float(v))
                    for t, v in zip(entry.get("Timestamps") or [],
                                    entry.get("Values") or [])
                ]
        return out

    return enumerator, reader
