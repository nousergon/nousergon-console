"""`console index` dump tests — the machine-readable §5.3 projection.

A config pointing at the example registry directory must produce a
versioned, machine-readable entity dump via the CLI entry (`console index`),
per the I327 deliverable-0 closes-when. These run the real entry point and
assert the dump is the index's entities and edges, JSON-serialized.
"""
from __future__ import annotations

import json

import yaml

from console.__main__ import main


def _write_config(tmp_path, registry_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({
        "registry": {"adapter": "yaml-directory", "path": str(registry_path),
                     "id_field": "component_id"},
    }))
    return cfg


def test_dump_is_versioned_json_of_the_index(capsys, tmp_path):
    reg = tmp_path / "registry.d"
    reg.mkdir()
    (reg / "comp-one.yaml").write_text("component_id: comp-one\nlifecycle: in-service\n")
    cfg = _write_config(tmp_path, reg)

    assert main(["--config", str(cfg), "index"]) == 0
    doc = json.loads(capsys.readouterr().out)

    assert doc["schema_version"] == 2
    ids = {e["id"] for e in doc["entities"]}
    assert ids == {"comp-one"}
    ent = doc["entities"][0]
    assert ent["kind"] == "component"
    assert ent["state"] == "UNREPORTED"  # declared, with nothing observing it yet
    assert set(ent["provenance"]) == {"source", "as_of", "evidence"}


def test_dump_includes_edges(capsys, tmp_path):
    reg = tmp_path / "registry.d"
    reg.mkdir()
    (reg / "comp-one.yaml").write_text("component_id: comp-one\nlifecycle: in-service\n")
    cfg = _write_config(tmp_path, reg)

    # A local-units adapter with a recorded enumerator is not injectable
    # through the CLI, so assert the edge projection shape via a fixture
    # index instead: the dump is a pure function of the built index.
    from console.index.graph import Index
    from console.model.entity import Edge, Entity, Provenance
    from console.model.envelope import AdapterResult, AdapterStatus
    from console.model.kinds import Kind, State

    idx = Index()
    idx.add_result(AdapterResult(
        name="schedules", status=AdapterStatus.OK,
        entities=(
            Entity(kind=Kind.RUN, id="run-1", state=State.HEALTHY,
                   provenance=Provenance(source="systemd")),
            Entity(kind=Kind.COMPONENT, id="comp-one.service", state=State.HEALTHY,
                   provenance=Provenance(source="systemd")),
        ),
        edges=(Edge(source="run-1", rel="belongs-to", target="comp-one.service"),),
    ))

    from console.__main__ import _dump

    doc = json.loads(_dump(idx))
    assert {"source": "run-1", "rel": "belongs-to", "target": "comp-one.service"} in doc["edges"]


def test_flag_order_does_not_matter(capsys, tmp_path):
    reg = tmp_path / "registry.d"
    reg.mkdir()
    (reg / "comp-one.yaml").write_text("component_id: comp-one\nlifecycle: in-service\n")
    cfg = _write_config(tmp_path, reg)

    # `console index --config X` and `console --config X index` must agree —
    # a sub-parser default must not clobber a flag given before the command.
    assert main(["index", "--config", str(cfg)]) == 0
    out1 = capsys.readouterr().out
    assert main(["--config", str(cfg), "index"]) == 0
    out2 = capsys.readouterr().out
    assert json.loads(out1) == json.loads(out2)
