"""yaml-directory adapter — the registry adapter.

Reads a directory of one-YAML-file-per-component (the fleet's observability
registry shape) and emits one Component entity per file. This is the
denominator adapter (`console-policy.md` §2.4): the set it returns is what the
console reports completeness *against*, and the difference between it and what
the telemetry adapters can see is the transparency-gap count.

It is generic — it knows "a directory of YAML files with a configurable id
field", nothing about any fleet's registry schema. Every literal (the path,
the id field) comes from configuration (§2.3).

Config (matches `config.example.yaml`'s `registry:` block):

    path:      directory of .yaml/.yml files, one per component
    id_field:  the key in each file holding the component id (e.g. component_id)
"""
from __future__ import annotations

import os
from typing import Any

import yaml

from ..model.entity import Edge, Entity, Provenance
from ..index.build import now_iso
from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass
from ..model.descriptor import Descriptor, parse as parse_descriptor
from ..model.kinds import DECLARED_LIFECYCLE_STATES, Kind, State

#: A registry directory is a DECLARATION (§2.5): it says what EXISTS and what
#: its declared lifecycle, owner and authority tier are. It does not say how
#: anything is doing — that is an observation's job, and _state_rank encodes it.
CLAIM_CLASS = ClaimClass.DECLARATION

name = "registry"
produces = ("component",)


def fetch(config: dict[str, Any]) -> AdapterResult:
    path = config.get("path")
    id_field = config.get("id_field", "component_id")
    if not path or not os.path.isdir(path):
        # A missing registry is a FAILED adapter state, not an empty surface
        # and not an exception (§2.3). With no rows we cannot even name the
        # known components, so the entity list is empty and the status carries
        # the finding.
        return AdapterResult(
            claim_class=CLAIM_CLASS,
            fetched_at=now_iso(),
            name=config.get("_name", name),
            status=AdapterStatus.FAILED,
            unavailable=("all",),
        )

    entities: list[Entity] = []
    # Descriptors are handed back on the result so the resolver can walk their
    # bindings (§2.6). They are NOT read here: §2.3 says an adapter reads one
    # source and nothing else knows that source at all, and an adapter that
    # followed its own rows' bindings would become a reader of every source in
    # the fleet.
    descriptors: list[Descriptor] = []
    known = config.get("known_drivers")
    for fname in sorted(os.listdir(path)):
        if not fname.endswith((".yaml", ".yml")):
            continue
        fpath = os.path.join(path, fname)
        row = yaml.safe_load(open(fpath)) or {}
        cid = row.get(id_field)
        if not cid:
            # A file with no id is a discovery problem, not a component — skip
            # loudly by surfacing it as an UNREGISTERED entity keyed by file.
            cid = f"__unregistered__/{fname}"
            state = State.UNREGISTERED
        else:
            state = _state_from_row(row)
        entities.append(
            Entity(
                kind=Kind.COMPONENT,
                id=str(cid),
                state=state,
                provenance=Provenance(
                    source=fpath,
                    as_of=_as_of(row),
                    evidence=f"file://{fpath}",
                ),
                facets=_facets(row),
                detail=_detail(row, fname),
            )
        )
        if row.get(id_field):
            descriptors.append(
                parse_descriptor(row, str(cid), source_file=fpath,
                                 known_drivers=known)
            )
    return AdapterResult(
        claim_class=CLAIM_CLASS,
        fetched_at=now_iso(),
        name=config.get("_name", name),
        status=AdapterStatus.OK,
        entities=tuple(entities),
        edges=tuple(_lineage_edges(descriptors)),
        descriptors=tuple(descriptors),
    )


def _lineage_edges(descriptors: list[Descriptor]) -> list[Edge]:
    """`produces` / `consumes` a descriptor declared (§6).

    Lineage is not a binding — nothing is read to satisfy it. It is the
    component stating its own edges, and `consumes` is the direction that
    exists nowhere unless somebody states it: the forward edge is a property of
    the producer and is usually written down, while "who breaks if this is
    stale" is not.
    """
    edges: list[Edge] = []
    for d in descriptors:
        for key in d.produces:
            edges.append(Edge(source=d.component_id, rel="produces", target=key))
        for key in d.consumes:
            edges.append(Edge(source=key, rel="consumed-by", target=d.component_id))
    return edges


def _detail(row: dict[str, Any], fname: str) -> dict[str, Any]:
    """What the declaration supplies beyond identity and lifecycle.

    `cadence_minutes` is here because §2.5's table names **cadence** as a thing
    a DECLARATION supplies and an observation does not — a substrate counter
    can say when something last ran, never when it was supposed to. Carrying it
    on the entity is what lets `index/cadence_state.py` separate `DISABLED`
    from `MISSED` at the merge, and what gives §9.6 a row it can audit at all:
    before this, `staleness_honesty`'s denominator could only ever contain
    entities from sources that happened to carry their own cadence, so a
    registry row's declared expectation was written down and never read.

    Absent, blank or non-positive values are omitted rather than passed
    through as `None`/`0` — a key present with an unusable value reads as
    declared and is not (§2.2's blank-required-field rule, one level down).
    """
    detail: dict[str, Any] = {
        "registry_file": fname,
        "produces": list(row.get("produces") or []),
        "consumes": list(row.get("consumes") or []),
    }
    cadence = row.get("cadence_minutes")
    try:
        minutes = float(cadence)
    except (TypeError, ValueError):
        minutes = 0.0
    if minutes > 0:
        detail["cadence_minutes"] = minutes
    return detail


def _facets(row: dict[str, Any]) -> dict[str, str]:
    """Map registry identity fields onto §2.2 facets, where present."""
    out: dict[str, str] = {}
    for field_name, facet in (
        ("owner", "owner"),
        ("substrate", "substrate"),
        ("owning_repo", "repo"),
        ("lifecycle", "lifecycle"),
        ("authority_tier", "authority_tier"),
    ):
        val = row.get(field_name)
        if isinstance(val, str) and val and val != "unknown":
            out[facet] = val
    return out


def _as_of(row: dict[str, Any]) -> str | None:
    """A registry file has no freshness stamp of its own; declare the absence
    (§2.3) rather than inventing one. The mtime is a file property, not the
    fact's as-of, so we return None and let the renderer mark it unverifiable."""
    return None


def _state_from_row(row: dict[str, Any]) -> State:
    """A registry row is a DECLARATION claim (§2.5), never an observation.

    Two halves, and observability-policy.md §8.3 fixes both:

    - A declared `lifecycle` of disabled/deprecated/retired maps to the
      matching state and NOTHING else may produce those three — "declared in
      the registry, never inferred". This is the `DISABLED` vs `MISSED` pair:
      a decision and a defect are indistinguishable from telemetry alone, so
      only the declaration can tell them apart.
    - An in-service row with no observation is `UNREPORTED` — "registered, in
      service, emitting nothing", which is the transparency-gap count itself.
      It is emphatically NOT `ABSENT` (the registry expects it and the
      substrate lacks it — a different finding, produced by the reconciler),
      and never a fall-through: a declaration claim losing to an observation
      claim at merge (§2.5) is how a row becomes HEALTHY.
    """
    lifecycle = str(row.get("lifecycle") or "in-service").strip().lower()
    declared = DECLARED_LIFECYCLE_STATES.get(lifecycle)
    if declared is not None:
        return declared
    return State.UNREPORTED
