"""declared-registry adapter — a declared registry of many entities of one
KIND, from one YAML document (`console-policy.md` §2.3, §2.5).

`yaml-directory` is the registry adapter for `console-policy.md`'s one closed
case: a directory of files, one Component per file. Two of this slice's
sources (`nousergon-console#58`) are the same DECLARATION shape over a
different file layout and a different target kind — one YAML document naming
*many* entities:

- an artifact registry (fresh/stale/**missing** load-bearing S3 keys) — the
  fleet's `ARTIFACT_REGISTRY.yaml`
- an observation registry (gated-off/gated-on/always-on rollouts) — the
  fleet's `OBSERVATION_REGISTRY.yaml`

Neither is "a directory of one-file-per-component" and neither produces a
Component, so this is a sibling adapter, not an edit to `yaml-directory`
(§2.3 — one adapter, one source shape).

**Why this is what makes "missing" computable at all.** An Artifact adapter
that only lists what a bucket prefix actually contains (`object-store`) can
never say a key is *missing* — it has no notion of what SHOULD be there. A
DECLARATION claim from this adapter, merged by identifier (§2.5) against an
OBSERVATION claim from `object-store` pointed at the same keys, is what turns
"never showed up in the listing" into a rendered fact rather than a silent
absence. When nothing observes a declared identifier, the single surviving
claim IS this adapter's own base state — deliberately the string `"absent"`,
the same token `drivers/object_store.py`'s own `object-store` driver already
uses for "declared and not there" (§5.1's second half: an Artifact carries the
source's own value verbatim, never the thirteen). `console-policy.md`'s
declared-only state guard (§8.3) does not apply here: that guard is scoped to
`COMPONENT_STATE_KINDS` (Component/Run) only, so a non-component kind's raw
state is free to mean whatever its own domain does.

**Why Decision entries need no merge at all.** `OBSERVATION_REGISTRY.yaml`'s
gate value (gated-off/gated-on/always-on) *is* the fact — nothing else
observes a rollout's own declared gate, so the lone DECLARATION claim IS the
rendered row, unmerged, by construction.

A target kind of Component or Run is accepted (§2.1's kinds are closed, not
this adapter's target list) but a declaration never supplies component
*state* (`docs/adapters.md`) — only a declared `lifecycle` of
disabled/deprecated/retired maps through, exactly as `yaml-directory` does;
anything else renders `UNREPORTED`, the honest "declared, unobserved" answer.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Mapping

import yaml

from .. import calendar_cadence
from ..model.entity import Edge, Entity, Provenance
from ..index.build import now_iso
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass
from ..model.kinds import COMPONENT_STATE_KINDS, DECLARED_LIFECYCLE_STATES, Kind, State
from ..trading_calendar import TradingDayChecker

#: A registry document is a DECLARATION (§2.5): it says what is EXPECTED to
#: exist, never how it is doing. Precedence — and the merge that computes
#: "missing" — follows from this exactly as it does for `yaml-directory`.
CLAIM_CLASS = ClaimClass.DECLARATION

name = "declared-registry"
#: Configurable per deployment (`kind:` in config) — every kind is listed
#: because the adapter itself is generic over which one it targets (§2.1's
#: kinds stay closed; this module simply does not fix its own target).
produces = ("artifact", "decision", "signal", "cycle", "incident", "component", "run")

#: Fields with a standard facet mapping, same convention as
#: `yaml_directory._facets` (§2.2) — reused rather than reinvented so two
#: registry shapes tag the same facet the same way.
_FACET_FIELDS: tuple[tuple[str, str], ...] = (
    ("owner", "owner"),
    ("substrate", "substrate"),
    ("owning_repo", "repo"),
    ("lifecycle", "lifecycle"),
    ("authority_tier", "authority_tier"),
    ("pipeline", "pipeline"),
    ("environment", "environment"),
)

#: Entry keys that are structural to this adapter and never land in `detail`
#: verbatim (they are surfaced as facets/edges instead, or are the id/state
#: fields themselves, handled separately).
_STRUCTURAL_KEYS = frozenset({"facets", "produces", "consumes"})


def fetch(
    config: dict[str, Any],
    now: datetime | None = None,
    trading_day_checker: TradingDayChecker | None = None,
) -> AdapterResult:
    path = config.get("path")
    kind_name = config.get("kind")
    kind = Kind.from_route(str(kind_name)) if kind_name else None

    if not path or not os.path.isfile(path):
        return _failed(config, ("all",))
    if kind is None:
        return _failed(config, ("kind",))

    id_field = config.get("id_field", "id")
    state_field = config.get("state_field")
    default_state = config.get("default_state", "declared")
    entries_field = config.get("entries_field")

    try:
        raw = yaml.safe_load(open(path)) or {}
    except Exception:
        return _failed(config, ("source",))

    if entries_field:
        raw = _dig(raw, str(entries_field))

    entities: list[Entity] = []
    edges: list[Edge] = []
    skipped = 0

    for mapping_key, entry in _entries(raw):
        eid = _entry_id(entry, mapping_key, id_field)
        if not eid:
            skipped += 1
            continue
        state = _entry_state(entry, kind, state_field, default_state)
        detail = _detail(entry, id_field, state_field)
        calendar_cadence.apply_declared_cadence(
            detail, entry, now=now, trading_day_checker=trading_day_checker,
        )
        entities.append(Entity(
            kind=kind,
            id=eid,
            state=state,
            provenance=Provenance(source=path, as_of=None, evidence=f"file://{path}"),
            facets=_facets(entry),
            detail=detail,
        ))
        for key in _string_list(entry.get("produces")):
            edges.append(Edge(source=eid, rel="produces", target=key))
        for key in _string_list(entry.get("consumes")):
            edges.append(Edge(source=key, rel="consumed-by", target=eid))

    unavailable = ("invalid-entries",) if skipped else ()
    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        name=config.get("_name", name),
        status=AdapterStatus.OK,
        entities=tuple(entities),
        edges=tuple(edges),
        unavailable=unavailable,
    )


def _failed(config: dict[str, Any], unavailable: tuple[str, ...]) -> AdapterResult:
    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        name=config.get("_name", name),
        status=AdapterStatus.FAILED,
        unavailable=unavailable,
    )


def _dig(raw: Any, dotted: str) -> Any:
    """Walk a dotted path (`observations.entries`) into a nested document."""
    cursor = raw
    for part in dotted.split("."):
        if not isinstance(cursor, Mapping) or part not in cursor:
            return []
        cursor = cursor[part]
    return cursor


def _entries(raw: Any) -> list[tuple[str | None, dict[str, Any]]]:
    """Both natural top-level shapes are accepted (mirrors
    `model/descriptor.py`'s "one mapping or a list of them" — a format that
    rejects a defensible spelling is a format people work around):

    - a list of entry mappings, each carrying its own id
    - a mapping of id -> entry, the id supplying `_entry_id`'s fallback
    """
    if isinstance(raw, list):
        return [(None, e) for e in raw if isinstance(e, Mapping)]
    if isinstance(raw, Mapping):
        return [(k, v) for k, v in raw.items() if isinstance(v, Mapping)]
    return []


def _entry_id(entry: Mapping[str, Any], mapping_key: str | None, id_field: str) -> str | None:
    val = entry.get(id_field)
    if val:
        return str(val)
    if mapping_key is not None:
        return str(mapping_key)
    return None


def _entry_state(
    entry: Mapping[str, Any], kind: Kind, state_field: str | None, default_state: str,
) -> State | str:
    lifecycle = str(entry.get("lifecycle") or "").strip().lower()
    declared = DECLARED_LIFECYCLE_STATES.get(lifecycle)

    if kind in COMPONENT_STATE_KINDS:
        # §2.5 / docs/adapters.md: "a declaration does not supply state" — the
        # ONLY thing a registry may say about a Component/Run's state is a
        # declared lifecycle disposition. Anything else is honestly
        # UNREPORTED (declared, unobserved), never a guessed raw value, and
        # never the escape hatch §8.3 forbids by name.
        return declared if declared is not None else State.UNREPORTED

    if declared is not None:
        # A non-component kind may still declare itself deliberately off —
        # rendered as the raw token, per §5.1's second half ("otherwise the
        # value itself"), never the enum instance.
        return declared.value
    if state_field and entry.get(state_field) is not None:
        return str(entry[state_field])
    return default_state


def _facets(entry: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for field_name, facet in _FACET_FIELDS:
        val = entry.get(field_name)
        if isinstance(val, str) and val and val != "unknown":
            out[facet] = val
    extra = entry.get("facets")
    if isinstance(extra, Mapping):
        out.update({str(k): str(v) for k, v in extra.items() if v is not None})
    return out


def _detail(entry: Mapping[str, Any], id_field: str, state_field: str | None) -> dict[str, Any]:
    reserved = _STRUCTURAL_KEYS | {id_field}
    if state_field:
        reserved = reserved | {state_field}
    # `fields` (§5.8's self-describing block) passes through verbatim like
    # every other adapter's detail — this function does not interpret it.
    return {k: v for k, v in entry.items() if k not in reserved}


def _string_list(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value if v)
