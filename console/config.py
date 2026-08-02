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

from .adapters import git_host, local_units, object_store, yaml_directory
from .index.graph import Index
from .model.envelope import AdapterStatus

#: Adapter registry — name → module with a `fetch` callable. Adding a source
#: is adding an adapter here (§2.3); no other wiring changes.
ADAPTERS = {
    "yaml-directory": yaml_directory,
    "git-host": git_host,
    "object-store": object_store,
    "local-units": local_units,
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
    # The registry adapter is configured under its own `registry:` block.
    reg = config.get("registry")
    if reg and reg.get("adapter") in ADAPTERS:
        module = ADAPTERS[reg["adapter"]]
        index.add_result(module.fetch({**reg, "_name": "registry"}))

    for entry in config.get("adapters", []) or []:
        if not entry.get("enabled"):
            continue
        kind = entry.get("kind")
        module = ADAPTERS.get(kind)
        if module is None:
            continue
        cfg = {**entry.get("config", {}), "_name": entry.get("name", kind)}
        index.add_result(module.fetch(cfg))
    return index
