"""Config loading — the console's only source of topology (§1.1, §2.3).

Everything specific to a deployment lives in `config.yaml` (gitignored) and
nowhere else. The console reads it at start and derives the index, navigation,
search and relation graph from whatever the enabled adapters return — so
adding a component or a source is a config/registry edit, never a code edit
(§3.5). This module loads the file and instantiates the enabled adapters; it
holds no topology of its own.
"""
from __future__ import annotations

from typing import Any

import yaml

from .adapters import (
    checks_envelope,
    git_host,
    local_units,
    object_store,
    state_machine,
    yaml_directory,
)
from .drivers import KNOWN_DRIVERS, resolve_bindings
from .index.build import Supervisor
from .index.graph import Index

#: Adapter registry — name → module with a `fetch` callable. Adding a source
#: is adding an adapter here (§2.3); no other wiring changes.
ADAPTERS = {
    "yaml-directory": yaml_directory,
    "git-host": git_host,
    "object-store": object_store,
    "local-units": local_units,
    "state-machine": state_machine,
    "checks-envelope": checks_envelope,
}


def load_config(path: str) -> dict[str, Any]:
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def build_index(config: dict[str, Any]) -> Index:
    """Run every enabled adapter once and fold its projection into the index.

    A FAILED adapter contributes its entities as UNREPORTED (handled in the
    index) and does not stop the other adapters — the surface degrades per
    source, never empties (§2.3).
    """
    index = Index()
    descriptors: list = []
    # The registry adapter is configured under its own `registry:` block.
    registry_entries = ([config["registry"]] if config.get("registry") else [])
    registry_entries.extend(config.get("registries") or [])
    for ordinal, reg in enumerate(registry_entries, start=1):
        name = reg.get("name", f"registry-{ordinal}")
        index.declare_registry(name)
        if reg.get("adapter") not in ADAPTERS:
            continue
        module = ADAPTERS[reg["adapter"]]
        result = module.fetch({
            **reg, "_name": name,
            "known_drivers": KNOWN_DRIVERS,
        })
        index.add_result(result)
        index.render_registry(name)
        descriptors.extend(result.descriptors)

    for entry in config.get("adapters", []) or []:
        if not entry.get("enabled"):
            continue
        kind = entry.get("kind")
        module = ADAPTERS.get(kind)
        if module is None:
            continue
        cfg = {**entry.get("config", {}), "_name": entry.get("name", kind)}
        index.add_result(module.fetch(cfg))

    # §2.6: every component's own descriptor said where its facts live. Walking
    # those bindings is what makes onboarding cost ONE FILE — a component whose
    # data lands somewhere no adapter points is read because it said where, not
    # because somebody added a prefix to this file.
    for result in resolve_bindings(descriptors, _driver_context(config)):
        index.add_result(result)
    return index


def _driver_context(config: dict[str, Any]) -> dict[str, Any]:
    """Console-level facilities a driver may need — never topology (§2.7).

    A clock and a staleness factor. Which bucket, group or table a component
    uses is in that component's descriptor, and nothing here may carry it.
    """
    console_cfg = config.get("console") or {}
    return {
        "staleness_factor": float(console_cfg.get("staleness_factor", 1.5)),
        # Values live only in the gitignored runtime config; descriptors carry
        # the stable key, never a DSN or password (§2.7).
        "sql_credentials": dict(config.get("sql_credentials") or {}),
        **(config.get("_driver_context") or {}),
    }


#: How often the index is rebuilt when the config does not say. Deliberately a
#: minute rather than an hour: §5.9's failure is a surface that looks live and
#: is not, and the cost of being wrong in the cheap direction is a few extra
#: reads of sources the console already reads.
DEFAULT_REFRESH_SECONDS = 60.0


def supervised_index(config: dict[str, Any]) -> Supervisor:
    """An index that rebuilds on a cadence and swaps atomically (§5.9).

    The whole of "the console renders, it never owns" (§5.6) made operational:
    nothing is persisted, so keeping the surface current is re-deriving it, and
    the only state that survives a rebuild is the reference to it.
    """
    refresh = float(
        (config.get("console") or {}).get("refresh_seconds")
        or DEFAULT_REFRESH_SECONDS
    )
    return Supervisor(lambda: build_index(config), refresh_seconds=refresh)
