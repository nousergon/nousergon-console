"""Config + the §3.5 acceptance test: adding a component needs no console edit.

The headline acceptance test from nous-ergon-ops-I327 and console-policy §3.5:
drop a new registry file in and the index, nav, search and relation graph pick
it up with zero code change — because they are all derived from the index.
"""
from __future__ import annotations

import os

import pytest
import yaml

from console.config import build_index
from console.model.kinds import Kind
from console.model.envelope import AdapterStatus


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


def test_each_adapter_fetch_records_its_own_elapsed(tmp_path):
    """I7124 deliverable 1: the 93.5s was one number. A cadence decision
    that cannot name the dominant source is a guess."""
    first, second = tmp_path / "first.d", tmp_path / "second.d"
    first.mkdir(); second.mkdir()
    _write_registry(str(first), {"comp-one": {}})
    _write_registry(str(second), {"comp-two": {}})
    index = build_index({"registries": [
        {"name": "first", "adapter": "yaml-directory", "path": str(first),
         "id_field": "component_id"},
        {"name": "second", "adapter": "yaml-directory", "path": str(second),
         "id_field": "component_id"},
    ]})
    by_name = {a.name: a for a in index.build_info.adapters}
    assert by_name["first"].elapsed_seconds is not None
    assert by_name["second"].elapsed_seconds is not None
    assert by_name["first"].elapsed_seconds >= 0
    assert by_name["second"].elapsed_seconds >= 0


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


def test_registry_manifest_widens_the_denominator_to_unwired_registries(tmp_path):
    """alpha-engine-config-I10035: the §7 denominator is the fleet's declared
    registry set, so a registry nobody wired to an adapter reads as missing
    instead of not being counted (live 2026-09-04: `1 of 1`, `missing: []`)."""
    reg = tmp_path / "observability.d"
    reg.mkdir()
    _write_registry(str(reg), {"comp-one": {}})
    index = build_index({
        "registries": [{"name": "fleet", "adapter": "yaml-directory",
                        "path": str(reg), "id_field": "component_id"}],
        "registry_manifest": [
            {"name": "fleet", "location": "governance/observability.d"},
            {"name": "policy-clauses", "location": "governance/policy-clauses.d"},
            "timeout-chains",
        ],
    })
    assert index.registry_coverage() == {
        "count": 1, "of": 3, "missing": ["policy-clauses", "timeout-chains"],
    }
    # An unwired manifest registry is a missing PAGE, not a failed read:
    # §9.1/§9.6's broken-registry population is unchanged by the manifest.
    assert index.broken_registries() == []


def test_registry_coverage_without_a_manifest_is_unchanged(tmp_path):
    reg = tmp_path / "first.d"
    reg.mkdir()
    _write_registry(str(reg), {"comp-one": {}})
    index = build_index({"registries": [
        {"name": "first", "adapter": "yaml-directory", "path": str(reg),
         "id_field": "component_id"},
    ]})
    assert index.registry_coverage() == {"count": 1, "of": 1, "missing": []}


@pytest.mark.parametrize("manifest", [
    "fleet", [{"location": "governance/x.d"}], [""], [None],
])
def test_a_malformed_registry_manifest_fails_by_name(manifest):
    from console.config import ConfigError, validate_config

    with pytest.raises(ConfigError, match="registry_manifest"):
        validate_config({"registry_manifest": manifest})
    # In the live build it fails only its own source, never the whole index.
    index = build_index({"registry_manifest": manifest}).finalize()
    adapters = {a.name: a for a in index.build_info.adapters}
    assert adapters["registry_manifest"].status == AdapterStatus.FAILED.value
    assert "registry_manifest" in adapters["registry_manifest"].unavailable[0]


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


def test_a_config_naming_an_unknown_adapter_is_a_visible_failed_source():
    """It used to `continue`: the configured source vanished from the surface
    AND from `build_info.adapters`, so nothing anywhere said it was not being
    read. That is exactly what happens when a config is applied ahead of the
    release implementing its adapter — the console renders a smaller fleet,
    silently, and looks healthy doing it (§2.3: a source that cannot be read is
    a declared state, never a dropped row)."""
    from console.config import build_index

    index = build_index({
        "adapters": [
            {"name": "future-source", "kind": "not-implemented-yet",
             "enabled": True, "config": {}},
        ],
    })
    fetches = {a.name: a for a in index.build_info.adapters}
    assert "future-source" in fetches
    assert fetches["future-source"].status == "failed"
    assert fetches["future-source"].unavailable == ("adapter:not-implemented-yet",)
