"""Build-time namespace uniqueness — `console-policy.md` §3.6.

`nousergon-console-I80`. §13 recorded this clause as *"Partial — asserted by the
wrong mechanism"* and cited `nousergon-console-I4` as its tracker; that issue
was about the entity index and closed 2026-08-10 without ever carrying this,
so the gap was unowned.

What these guard is a **shadow**, not a crash. Two registry rows declaring one
`component_id` with the same kind merge silently today: the losing row's owner,
authority tier and declared lifecycle vanish and nothing says so. §3.6 wants
that to fail the build. The runtime `NamespaceCollision` path is a different
question — several *sources* describing one identifier at refresh time — and
these tests assert it is left alone, because a serving index that refuses to
build over a shadowed row converts an authoring mistake into an outage.
"""
from __future__ import annotations

import pytest
import yaml

from console.index.namespace import (
    DeclaredNamespaceCollision,
    assert_unique,
    check,
)


def _registry(tmp_path, name, rows, id_field="component_id"):
    d = tmp_path / name
    d.mkdir()
    for filename, body in rows.items():
        (d / filename).write_text(yaml.safe_dump(body))
    return {"name": name, "adapter": "yaml-directory", "path": str(d),
            "id_field": id_field}


def test_a_unique_namespace_passes(tmp_path):
    config = {"registry": _registry(tmp_path, "fleet", {
        "a.yaml": {"component_id": "alpha"},
        "b.yaml": {"component_id": "beta"},
    })}
    assert check(config) == []
    assert_unique(config)  # must not raise


def test_one_identifier_in_two_files_is_a_collision(tmp_path):
    """The shadow case. Both files declare `alpha`; today the second wins and
    the first's declaration is invisible."""
    config = {"registry": _registry(tmp_path, "fleet", {
        "a.yaml": {"component_id": "alpha", "owner": "brian"},
        "duplicate.yaml": {"component_id": "alpha", "owner": "somebody-else"},
    })}
    collisions = check(config)
    assert len(collisions) == 1
    assert collisions[0].identifier == "alpha"
    assert collisions[0].scope == "component"
    with pytest.raises(DeclaredNamespaceCollision):
        assert_unique(config)


def test_the_error_names_every_file_the_identifier_came_from(tmp_path):
    """A build error that says 'alpha is duplicated' and stops sends the reader
    back to grep. It must name the files."""
    config = {"registry": _registry(tmp_path, "fleet", {
        "a.yaml": {"component_id": "alpha"},
        "z.yaml": {"component_id": "alpha"},
    })}
    with pytest.raises(DeclaredNamespaceCollision) as exc:
        assert_unique(config)
    message = str(exc.value)
    assert "a.yaml" in message and "z.yaml" in message


def test_a_collision_ACROSS_two_registries_is_caught(tmp_path):
    """The case that shadows hardest, and the one a per-directory check misses:
    two registries each declaring the same component."""
    config = {
        "registry": _registry(tmp_path, "fleet", {"a.yaml": {"component_id": "alpha"}}),
        "registries": [
            _registry(tmp_path, "second", {"a.yaml": {"component_id": "alpha"}}),
        ],
    }
    collisions = check(config)
    assert [c.identifier for c in collisions] == ["alpha"]


def test_two_registries_sharing_a_name_collide_on_their_generated_page(tmp_path):
    """§7 gives every registry exactly one generated index page, keyed by name.
    Two registries under one name means one page renders and the other silently
    does not exist."""
    config = {"registries": [
        _registry(tmp_path, "fleet", {"a.yaml": {"component_id": "alpha"}}),
        {"name": "fleet", "adapter": "yaml-directory",
         "path": str(tmp_path / "fleet"), "id_field": "component_id"},
    ]}
    scopes = {c.scope for c in check(config)}
    assert "registry" in scopes


def test_every_collision_is_reported_not_just_the_first(tmp_path):
    """A build error reporting one of four duplicates gets fixed four times."""
    config = {"registry": _registry(tmp_path, "fleet", {
        "a.yaml": {"component_id": "alpha"},
        "a2.yaml": {"component_id": "alpha"},
        "b.yaml": {"component_id": "beta"},
        "b2.yaml": {"component_id": "beta"},
    })}
    assert sorted(c.identifier for c in check(config)) == ["alpha", "beta"]


def test_a_malformed_file_is_not_reported_as_a_collision(tmp_path):
    """A YAML typo is a different finding with a different owner. Failing here
    on it would make a parse error look like a namespace collision — the way a
    check earns an exemption list and then stops checking."""
    d = tmp_path / "fleet"
    d.mkdir()
    (d / "ok.yaml").write_text(yaml.safe_dump({"component_id": "alpha"}))
    (d / "broken.yaml").write_text("{{{ not yaml")
    (d / "list.yaml").write_text(yaml.safe_dump(["not", "a", "mapping"]))
    config = {"registry": {"name": "fleet", "adapter": "yaml-directory",
                           "path": str(d), "id_field": "component_id"}}
    assert check(config) == []


def test_a_missing_registry_directory_is_not_a_collision(tmp_path):
    """An unreadable registry is the `yaml-directory` adapter's FAILED state
    (§2.3), reported there. This check must not double-report it as something
    it is not."""
    config = {"registry": {"name": "fleet", "adapter": "yaml-directory",
                           "path": str(tmp_path / "does-not-exist"),
                           "id_field": "component_id"}}
    assert check(config) == []


# ------------------------ alias ids (alpha-engine-config-I8779) -------------


def test_an_alias_colliding_with_another_rows_id_is_a_collision(tmp_path):
    config = {"registry": _registry(tmp_path, "fleet", {
        "a.yaml": {"component_id": "comp-a", "alias_ids": ["comp-b"]},
        "b.yaml": {"component_id": "comp-b"},
    })}
    collisions = check(config)
    assert [c.identifier for c in collisions] == ["comp-b"]
    with pytest.raises(DeclaredNamespaceCollision):
        assert_unique(config)


def test_two_rows_aliasing_the_same_id_is_a_collision(tmp_path):
    config = {"registry": _registry(tmp_path, "fleet", {
        "a.yaml": {"component_id": "comp-a", "alias_ids": ["shared-alias"]},
        "b.yaml": {"component_id": "comp-b", "alias_ids": ["shared-alias"]},
    })}
    assert [c.identifier for c in check(config)] == ["shared-alias"]


def test_an_alias_with_no_collision_passes(tmp_path):
    config = {"registry": _registry(tmp_path, "fleet", {
        "a.yaml": {"component_id": "comp-a", "alias_ids": ["comp-a-alias"]},
        "b.yaml": {"component_id": "comp-b"},
    })}
    assert check(config) == []


def test_a_custom_id_field_is_honoured(tmp_path):
    """The id field is configuration (§2.3) — a check hardcoding
    `component_id` would silently pass on any registry that does not use it."""
    config = {"registry": _registry(tmp_path, "fleet", {
        "a.yaml": {"artifact_key": "s3://b/k"},
        "b.yaml": {"artifact_key": "s3://b/k"},
    }, id_field="artifact_key")}
    assert [c.identifier for c in check(config)] == ["s3://b/k"]


def test_the_shipped_example_configuration_is_clean():
    """The repo's own published configuration must pass the check it ships —
    otherwise the first thing an outside user runs is a failure (§1.1)."""
    from console.config import load_config

    assert check(load_config("config.example.yaml")) == []


class TestTheRuntimePathIsUnchanged:
    """§3.6's build-time half must not be bought by weakening the refresh-time
    half. These are the two behaviours `merge.py` and `build.py` guarantee, and
    this check has no business altering either."""

    def test_the_runtime_collision_type_still_exists_and_is_distinct(self):
        from console.index.merge import NamespaceCollision

        assert NamespaceCollision is not DeclaredNamespaceCollision

    def test_building_an_index_never_calls_the_build_time_check(self):
        """Deliberately NOT wired into `build_index`. A serving index that
        refuses to build over a shadowed row converts an authoring mistake into
        an outage — the failure `merge.py`'s docstring was written against."""
        import inspect

        from console import config as config_module

        source = inspect.getsource(config_module.build_index)
        assert "assert_unique" not in source
        assert "namespace" not in source
