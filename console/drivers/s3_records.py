"""`s3-records` driver — record fan-out from one descriptor-declared document.

The driver-direction twin of `console/adapters/s3_records.py` (§2.7's
adapter/driver pair, same shape as `object-store`'s two modules): the adapter
enumerates a whole prefix the console's own config names; this driver reads
ONE document a component's own descriptor points at, and applies the
identical fan-out grammar to project one entity per record.

    metrics:
      - driver: s3-records
        key: "s3://research-bucket/evaluator/2026-08-16/report_card.json"
        kind: signal                    # one Signal per (tile, component)
        records_path: tiles.*.components
        group_field: tile
        id_template: "{tile}:{name}"
        state_field: status
        fields:
          value: {path: value}
          ci_low: {path: ci_low}
          ci_high: {path: ci_high}
          n_samples: {path: n_samples, render: count}
          target: {path: target}
          red_line: {path: red_line}
          trend: {path: trend_decoration, render: text}
          status_reason: {path: status_reason, render: text}
        cadence_minutes: 1440

A `records` source over a GROWING series additionally declares a bounded
window (`alpha-engine-config-I9618`), or it mints one entity per record
forever — `trades/eod_pnl.csv` is 120 trading sessions today and grows ~250 a
year:

    metrics:
      - driver: s3-records
        key: "s3://alpha-engine-research/trades/eod_pnl.csv"
        format: csv
        kind: signal
        limit: 30                       # at most this many entities
        order: last                     # ...from THIS end of the series
        id_template: "portfolio-alpha:{date}"

`order` has no default and is required whenever `limit` is set: a `limit: 30`
that silently took the OLDEST 30 rows would render a true number about a
window nobody asked for. The grammar lives in `console/records_shape.py`
alongside the fan-out itself, so the adapter and this driver cannot end up
with different notions of a bounded window (§2.3).

Filed as `nousergon-console#98` (blocking `alpha-engine-config-I7477`): the
report-card v2 body (`report_card.json`'s `tiles.*.components`) is exactly the
adapter's "grouped" shape — a dict-then-array fan-out with the tile name
injected via `group_field` — but no DESCRIPTOR-bindable driver reached it, so
a component whose Signals live in a body like this could only be onboarded
through a console-config edit (`config.example.yaml`'s `crucible-report-card`
block), which is the §2.6 defect the driver layer exists to remove.

**The fan-out grammar itself is not reimplemented here.** It is read from
`console/records_shape.py`, the module both this driver and the `s3-records`
**adapter** import — one grammar, two callers, per §2.3's "an adapter [or
driver] never forks a shape it already reads elsewhere" and the precedent of
`nousergon-console#79`'s adapter-layer consolidation.

Unlike the adapter, there is no key-pattern regex here (one document, not a
prefix listing) — so there are no capture-group `groups`; `id_template` and
`fields` resolve only against the document's own body-level and per-record
values. A component needing several documents combined into one binding
should use `document-fields` (§2.6's other driver for "several JSON bodies,
named fields") — this driver is for the fan-OUT case, one document minting
MANY entities, which `document-fields` structurally cannot do.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ..model.descriptor import Binding
from ..model.entity import Edge, Entity, Provenance
from ..model.kinds import Kind
from ..records_shape import (
    RecordsSelectorError, build_fields, flat_context, get_path, project,
    resolve_facets, resolve_id, resolve_state,
)
from .base import Cost, DriverResult

name = "s3-records"
#: Fully generic over `kind`, same as the adapter — every §2.1 kind is
#: reachable depending on the descriptor's own `kind` declaration.
kinds = ("component", "run", "cycle", "artifact", "signal", "decision", "incident")
cost = Cost.CHEAP  # one document read, not a prefix scan


def read(binding: Binding, context: dict[str, Any]) -> DriverResult:
    key = binding.spec.get("key")
    if not key:
        return DriverResult.failed(
            binding, "s3-records requires a `key` — say which document",
            unavailable=("key",),
        )
    kind = _resolve_kind(binding.spec.get("kind"))
    if kind is None:
        return DriverResult.failed(
            binding,
            f"s3-records requires a `kind` naming one of the seven entity "
            f"kinds (got {binding.spec.get('kind')!r})",
            unavailable=("kind",),
        )

    reader: Callable[[str], Any] | None = context.get("document_reader")
    if reader is None:
        return DriverResult.failed(
            binding,
            "no document reader available — the binding is declared and "
            "unreadable, which is a different finding from the document "
            "being absent",
            unavailable=("reader",),
        )

    try:
        raw = reader(str(key))
    except FileNotFoundError:
        return DriverResult.failed(
            binding, f"no document at {key} — declared and never written",
            unavailable=("document",),
        )
    except Exception as exc:  # noqa: BLE001 - a state, never an exception (§2.3)
        return DriverResult.failed(binding, f"{type(exc).__name__}: {exc}")

    fmt = str(binding.spec.get("format", "json"))
    try:
        records, body_root = project(
            raw, fmt, binding.spec.get("records_path"),
            binding.spec.get("array_fields"), binding.spec.get("group_field"),
            binding.spec.get("limit"), binding.spec.get("order"),
        )
    except RecordsSelectorError as exc:
        # A malformed `limit`/`order` is a defect in the DESCRIPTOR, not a
        # body that failed to match it. Saying so is the difference between
        # "fix your document" and "fix your binding" (`console-policy.md`
        # §3.9 — when something is not on the surface, the surface says why).
        return DriverResult.failed(
            binding,
            f"the bounded-records selector on {key} is not honourable: {exc}",
            unavailable=("selector",),
        )
    except (TypeError, ValueError, KeyError) as exc:
        return DriverResult.failed(
            binding,
            f"the document at {key} did not match its declared shape: {exc}",
            unavailable=("body",),
        )

    stat: Callable[[str], str | None] | None = context.get("object_stat")
    last_modified: str | None = None
    if stat is not None:
        try:
            last_modified = stat(str(key))
        except Exception:  # noqa: BLE001 - freshness is best-effort here
            last_modified = None

    now = context.get("now") or datetime.now(timezone.utc)
    cadence_seconds = _cadence_seconds(binding.spec)
    staleness_factor = float(
        binding.spec.get("staleness_factor", context.get("staleness_factor", 1.5))
    )
    source_label = f"s3-records:{key}"

    entities: list[Entity] = []
    partial = False
    for record in records:
        entity = _one_entity(
            record, body_root, kind, key, source_label, last_modified,
            staleness_factor, cadence_seconds, now, binding,
        )
        if entity is None:
            partial = True
            continue
        entities.append(entity)

    # The component produces the document (§3.3, §6) — always declared, even
    # when the fan-out below is empty, so a document with zero matching
    # records is still linked from its component.
    edges = [Edge(source=binding.component_id, rel="produces", target=str(key))]
    # nousergon-console-alpha-engine-config-I8768: when `records_path` fans
    # one document out into MANY entities (`kind: signal`'s per-tile rows,
    # for instance), the document-level edge above targets the KEY, never any
    # of the record ids the fan-out actually mints — so every fanned-out
    # entity carried zero inbound edges regardless of how many bindings
    # pointed at its document. Each record's own id is already this driver's
    # own read (`entity.id`, from `id_template` over the SAME record the
    # entity above was built from), so declaring it here invents no
    # identifier (§2.3). Skipped when the id already equals the document key
    # (the non-fan-out, one-entity-per-document case) — that edge is the one
    # above already, and a second copy would be a duplicate, not a new fact.
    edges.extend(
        Edge(source=binding.component_id, rel="produces", target=e.id)
        for e in entities
        if e.id != str(key)
    )
    return DriverResult(
        binding=binding, entities=tuple(entities), edges=tuple(edges),
        cadence_seconds=cadence_seconds,
        unavailable=("record",) if partial else (),
    )


def _one_entity(
    record: dict,
    body_root: dict,
    kind: Kind,
    key: str,
    source_label: str,
    last_modified: str | None,
    staleness_factor: float,
    cadence_seconds: float | None,
    now: datetime,
    binding: Binding,
) -> Entity | None:
    spec = binding.spec
    path_root = {**body_root, **record}
    # No key-pattern capture groups (one document, not a prefix listing) —
    # only body-level and record-level scalars are reachable.
    context = flat_context({}, body_root, record)

    id_template = spec.get("id_template", "")
    entity_id = resolve_id(str(id_template), context)
    if entity_id is None:
        return None

    as_of_field = spec.get("as_of_field")
    as_of = (
        str(get_path(path_root, as_of_field))
        if as_of_field and get_path(path_root, as_of_field) is not None
        else last_modified
    )

    evidence_template = spec.get("evidence_template")
    evidence = (
        str(evidence_template).format(**context) if evidence_template else str(key)
    )

    state = resolve_state(
        kind, spec.get("state_field"), spec.get("state_default"),
        spec.get("state_map"), path_root, as_of, cadence_seconds,
        staleness_factor, now,
    )

    fields_out = build_fields(path_root, spec.get("fields"), spec.get("question"))

    # Same grammar as the adapter, from the one place it is written (§2.3).
    facets = resolve_facets(spec.get("facets"), path_root)

    return Entity(
        kind=kind,
        id=entity_id,
        state=state,
        provenance=Provenance(source=source_label, as_of=as_of, evidence=evidence),
        facets=facets,
        detail={"fields": fields_out, "declared_by": binding.component_id},
    )


def _resolve_kind(raw: Any) -> Kind | None:
    if not raw:
        return None
    try:
        return Kind(str(raw))
    except ValueError:
        return None


def _cadence_seconds(spec: dict[str, Any]) -> float | None:
    for key, mult in (("cadence_seconds", 1.0), ("cadence_minutes", 60.0),
                      ("cadence_hours", 3600.0)):
        raw = spec.get(key)
        if raw:
            try:
                return float(raw) * mult
            except (TypeError, ValueError):
                return None
    return None
