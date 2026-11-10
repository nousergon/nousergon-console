"""The YAML registry reader accepts list and mapping shaped documents."""
from __future__ import annotations

from pathlib import Path

import pytest

from console import registry_document as registry


def test_load_parses_yaml(tmp_path: Path) -> None:
    path = tmp_path / "registry.yaml"
    path.write_text("entries:\n  - name: alpha\n    count: 2\n", encoding="utf-8")
    assert registry.load(str(path)) == {"entries": [{"name": "alpha", "count": 2}]}


def test_load_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")
    assert registry.load(str(path)) == {}


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        registry.load(str(tmp_path / "missing.yaml"))


def test_dig_returns_document_for_no_path() -> None:
    raw = {"entries": [{"id": "alpha"}]}
    assert registry.dig(raw, None) is raw
    assert registry.dig(raw, "") is raw


def test_dig_resolves_nested_path_and_handles_unresolved_parts() -> None:
    raw = {"observations": {"entries": [{"id": "alpha"}]}}
    assert registry.dig(raw, "observations.entries") == [{"id": "alpha"}]
    assert registry.dig(raw, "missing.entries") == []
    assert registry.dig(raw, "observations.missing") == []
    assert registry.dig(raw, "observations.entries.id") == []
    assert registry.dig({"observations": None}, "observations.entries") == []


def test_entries_filters_list_items() -> None:
    assert registry.entries([{"id": "alpha"}, None, "ignored", 3, [], {}]) == [
        (None, {"id": "alpha"}), (None, {}),
    ]


def test_entries_filters_mapping_values_and_stringifies_keys() -> None:
    assert registry.entries({"alpha": {"count": 2}, 7: {}, "skip": [1]}) == [
        ("alpha", {"count": 2}), ("7", {}),
    ]


def test_entries_rejects_other_shapes() -> None:
    for raw in (None, "alpha", 7, ({"id": "alpha"},)):
        assert registry.entries(raw) == []


def test_entry_id_prefers_configured_field_and_stringifies_it() -> None:
    assert registry.entry_id({"name": "alpha", "id": "ignored"}, "fallback", "name") == "alpha"
    assert registry.entry_id({"name": 42}, "fallback", "name") == "42"
    assert registry.entry_id({}, "fallback", "name") == "fallback"
    assert registry.entry_id({}, None, "name") is None


def test_falsy_entry_id_falls_back_to_mapping_key() -> None:
    for value in (None, "", 0, False):
        assert registry.entry_id({"name": value}, "fallback", "name") == "fallback"
        assert registry.entry_id({"name": value}, None, "name") is None


def test_read_list_drops_entries_without_resolvable_ids(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- name: alpha\n- owner: team\n- name: ''\n- name: 42\n- ignored\n",
                    encoding="utf-8")
    assert registry.read(str(path), None, "name") == [
        ("alpha", {"name": "alpha"}), ("42", {"name": 42}),
    ]


def test_read_mapping_uses_keys_and_explicit_ids(tmp_path: Path) -> None:
    path = tmp_path / "mapping.yaml"
    path.write_text("alpha:\n  owner: team\nbeta:\n  name: explicit\n7: {}\nskip: ignored\n",
                    encoding="utf-8")
    assert registry.read(str(path), "", "name") == [
        ("alpha", {"owner": "team"}), ("explicit", {"name": "explicit"}), ("7", {}),
    ]


def test_read_walks_configured_entries_field(tmp_path: Path) -> None:
    path = tmp_path / "nested.yaml"
    path.write_text("observations:\n  entries:\n    - name: alpha\n    - owner: team\n",
                    encoding="utf-8")
    assert registry.read(str(path), "observations.entries", "name") == [
        ("alpha", {"name": "alpha"}),
    ]
    assert registry.read(str(path), "observations.missing", "name") == []
