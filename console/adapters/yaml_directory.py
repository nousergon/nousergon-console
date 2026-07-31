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

from ..model.entity import Entity, Provenance
from ..model.envelope import AdapterResult, AdapterStatus
from ..model.kinds import Kind, State

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
            name=config.get("_name", name),
            status=AdapterStatus.FAILED,
            unavailable=("all",),
        )

    entities: list[Entity] = []
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
                detail={"registry_file": fname},
            )
        )
    return AdapterResult(
        name=config.get("_name", name),
        status=AdapterStatus.OK,
        entities=tuple(entities),
    )


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
    """A registry row is a declaration, not telemetry. The row's own state is
    UNKNOWN (declared, not measured) — the telemetry adapters are what promote
    a component to HEALTHY/UNREPORTED. A lifecycle of not-in-service maps to
    ABSENT so retired components don't page the exception list."""
    lifecycle = row.get("lifecycle")
    if lifecycle and lifecycle != "in-service":
        return State.ABSENT
    return State.UNKNOWN
