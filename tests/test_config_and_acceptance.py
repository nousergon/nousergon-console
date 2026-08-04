"""Config + the §3.5 acceptance test: adding a component needs no console edit.

The headline acceptance test from nous-ergon-ops-I327 and console-policy §3.5:
drop a new registry file in and the index, nav, search and relation graph pick
it up with zero code change — because they are all derived from the index.
"""
from __future__ import annotations

import os

import yaml

from console.config import build_index
from console.model.kinds import Kind


def _write_registry(dirpath, components):
    for cid, extra in components.items():
        row = {"component_id": cid, "lifecycle": "in-service", "owner": "x", **extra}
        with open(os.path.join(dirpath, f"{cid}.yaml"), "w") as fh:
            yaml.safe_dump(row, fh)


def test_build_index_from_registry_config(tmp_path):
    reg = tmp_path / "registry.d"
    reg.mkdir()
    _write_registry(str(reg), {"comp-one": {}, "comp-two": {}})
    config = {"registry": {"adapter": "yaml-directory", "path": str(reg),
                           "id_field": "component_id"}}
    index = build_index(config)
    assert {e.id for e in index.of_kind(Kind.COMPONENT)} == {"comp-one", "comp-two"}


def test_multiple_declared_registries_are_discovered_without_console_wiring(tmp_path):
    first, second = tmp_path / "first.d", tmp_path / "second.d"
    first.mkdir(); second.mkdir()
    _write_registry(str(first), {"comp-one": {}})
    _write_registry(str(second), {"comp-two": {}})
    index = build_index({"registries": [
        {"adapter": "yaml-directory", "path": str(first), "id_field": "component_id"},
        {"adapter": "yaml-directory", "path": str(second), "id_field": "component_id"},
    ]})
    assert {e.id for e in index.of_kind(Kind.COMPONENT)} == {"comp-one", "comp-two"}


def test_registry_coverage_names_a_declared_registry_without_a_generated_page(tmp_path):
    reg = tmp_path / "first.d"
    reg.mkdir()
    _write_registry(str(reg), {"comp-one": {}})
    index = build_index({"registries": [
        {"name": "first", "adapter": "yaml-directory", "path": str(reg),
         "id_field": "component_id"},
        {"name": "missing", "adapter": "not-an-adapter"},
    ]})
    assert index.registry_coverage() == {
        "count": 1, "of": 2, "missing": ["missing"],
    }


def test_adding_a_component_requires_no_console_edit(tmp_path):
    """§3.5 acceptance: a component added to the registry appears in the index
    with no change to console code — nav, search and relations follow because
    they are all projections of the same derived index."""
    reg = tmp_path / "registry.d"
    reg.mkdir()
    _write_registry(str(reg), {"comp-one": {}})
    config = {"registry": {"adapter": "yaml-directory", "path": str(reg),
                           "id_field": "component_id"}}

    before = build_index(config)
    assert "comp-new" not in {e.id for e in before.all()}

    # The ONLY change is a new registry file — no console edit.
    _write_registry(str(reg), {"comp-new": {}})
    after = build_index(config)
    ids = {e.id for e in after.all()}
    assert "comp-new" in ids
    # And it is reachable on the structure path (present in the generated index).
    assert after.entity("comp-new") is not None


def test_disabled_adapters_are_not_run(tmp_path):
    reg = tmp_path / "registry.d"
    reg.mkdir()
    _write_registry(str(reg), {"comp-one": {}})
    config = {
        "registry": {"adapter": "yaml-directory", "path": str(reg),
                     "id_field": "component_id"},
        "adapters": [
            {"name": "checks", "kind": "object-store", "enabled": False,
             "config": {"bucket": "b", "key_pattern": "x"}},
        ],
    }
    index = build_index(config)
    # Only the registry's one component; the disabled object-store ran nothing.
    assert {e.id for e in index.all()} == {"comp-one"}
