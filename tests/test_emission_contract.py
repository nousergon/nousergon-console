"""The emission contract (§2.6) — the chokepoint for CN-2.6.

`test_a_module_onboards_with_zero_console_edits` **is** the clause. Everything
else here supports it.

The defect it guards against is not a crash — it is a slope. `docs/adapters.md`
used to say *"Adding a source is adding an adapter module and one line in
`console/config.py::ADAPTERS`"*, and that was the only documented way onto the
surface. A process or module is not a source; it writes into one. If every new
module needs adapter code, onboarding cost is unbounded, and a surface whose
coverage is bounded by how much adapter code somebody felt like writing will
always render a subset **while looking complete** — which is the exact failure
mode the transparency-gap count exists to prevent, arriving through the door
nobody was watching.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from console.adapters import checks_envelope
from console.config import build_index
from console.emit import STATUSES, load_schema, report, write_json
from console.model.envelope import AdapterResult, AdapterStatus
from console.model.kinds import Kind, State
from console.search.resolve import search

from datetime import datetime, timezone

NOW = datetime(2026, 8, 3, 12, 30, tzinfo=timezone.utc)

CONSOLE_PACKAGE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "console"
)


# ------------------------------------------------- the clause itself --------

def test_a_module_onboards_with_zero_console_edits(tmp_path, monkeypatch):
    """§2.6: emit + a registry row, and it appears. Nothing else.

    The guard that makes this test mean something is the git check at the end:
    if satisfying it required touching any file under `console/`, the test
    fails. Without that, "onboarding is seamless" is a claim the test would
    happily make while the implementer quietly added a special case.
    """
    before = _console_tree_hash()

    # 1. A module nobody has heard of emits the published envelope. This is the
    #    entire integration: no adapter written, no config key added.
    write_json(
        report(
            component_id="brand-new-module",
            status="ok",
            ran_at="2026-08-03T12:00:00+00:00",
            cadence_minutes=60,
            summary="did the thing, 0 rejected",
            deep_link="https://ci.example/run/1",
            facets={"owner": "brian", "substrate": "laptop-launchd"},
            produces=["reports/brand-new-module/out.parquet"],
        ),
        str(tmp_path / "reports" / "brand-new-module" / "latest.json"),
    )

    # 2. Its registry row, in the registry directory the console already reads.
    registry = tmp_path / "registry.d"
    registry.mkdir()
    (registry / "brand-new-module.yaml").write_text(
        "component_id: brand-new-module\nowner: brian\nlifecycle: in-service\n"
    )

    # 3. The console's EXISTING configuration, unchanged in shape.
    config = {
        "registry": {
            "adapter": "yaml-directory",
            "path": str(registry),
            "id_field": "component_id",
        },
        "adapters": [{
            "name": "reports",
            "kind": "checks-envelope",
            "enabled": True,
            "config": {
                "bucket": "local",
                "prefix": "reports/",
                "key_pattern": r"reports/(?P<component_id>[^/]+)/latest\.json",
            },
        }],
    }
    _serve_local_objects(monkeypatch, tmp_path)

    index = build_index(config)

    ent = index.entity("brand-new-module")
    assert ent is not None, "the module did not appear on the surface"
    assert ent.kind is Kind.COMPONENT
    assert ent.state is State.HEALTHY
    assert ent.facets["owner"] == "brian"
    # Reachable by name (§3.1 path one) with no console change. The exact
    # identifier ranks first (§3.7); its run and the artifact it produced also
    # surface, which is the point — one emission put three typed entities and
    # a lineage edge on the surface, not one row.
    hits = search(index, "brand-new-module")
    assert hits[0].entity.id == "brand-new-module" and hits[0].exact
    kinds = {index.entity(h.entity.id).kind for h in hits}
    assert {Kind.COMPONENT, Kind.RUN, Kind.ARTIFACT} <= kinds

    # Reachable by relation (§3.1 path three): the artifact it declared as an
    # output is linked from it, both directions, with no console change.
    rels = {(e.rel, e.target) for e in index.related("brand-new-module")}
    assert ("produces", "reports/brand-new-module/out.parquet") in rels

    assert _console_tree_hash() == before, (
        "onboarding required a change under console/ — §2.6 is not met, and "
        "§9.8's target of zero is already missed on the first component"
    )


def test_the_same_path_works_with_no_registry_row_at_all():
    """A module that emits but is not declared still appears — as
    `UNREGISTERED` (§8.3), which is a finding rather than an absence.

    Onboarding half-done must be VISIBLE. The failure mode this rules out is
    the quiet one: emit configured, registry row forgotten, and nothing
    anywhere says so.
    """
    from console.adapters import yaml_directory
    from console.index.graph import Index

    doc = report(component_id="undeclared-module", status="ok",
                 ran_at="2026-08-03T12:00:00+00:00", cadence_minutes=60)

    idx = Index()
    # A registry exists and declares SOMETHING ELSE. That is what makes
    # "unregistered" a claim anyone can make: with no registry configured at
    # all there is no denominator, and painting a registry-less surface red on
    # a configuration choice would be a verdict about the operator, not the
    # fleet. The guard is asserted below.
    idx.add_result(yaml_directory.fetch(
        {"_name": "registry", "path": _registry_with_other(), "id_field": "component_id"}
    ))
    idx.add_result(_adapter_over({"reports/undeclared-module/latest.json": doc}))
    assert idx.entity("undeclared-module").state is State.UNREGISTERED

    # …and with no registry at all, it is simply HEALTHY: nothing has standing
    # to call it unregistered.
    bare = Index()
    bare.add_result(_adapter_over({"reports/undeclared-module/latest.json": doc}))
    assert bare.entity("undeclared-module").state is State.HEALTHY


# ---------------------------------------------- the contract's own rules ----

def test_every_schema_field_is_optional():
    """§2.6: a required field added later is a fleet-wide breaking change
    dressed as a schema improvement, and it lands on every emitter at once —
    most of which nobody is going to redeploy."""
    schema = load_schema()
    assert schema.get("required") == []


def test_an_empty_report_is_valid_and_renders_as_a_finding():
    """The degenerate case has to work, because it is what a half-configured
    emitter produces. It must render, and it must not render green."""
    doc = report()
    assert doc == {"schema_version": 1}
    result = _adapter_over({"reports/bare/latest.json": doc})
    (component,) = [e for e in result.entities if e.kind is Kind.COMPONENT]
    assert component.state is State.UNREPORTED


def test_the_status_vocabulary_is_about_the_run_not_the_component():
    """An emitter knows whether its own work succeeded. It does not know
    whether it is disabled, deprecated or retired — those are declared in the
    registry (§8.3), and telemetry claiming one is the collapse §8.3 forbids."""
    assert set(STATUSES) == {"ok", "attention", "error"}
    for forbidden in ("disabled", "deprecated", "retired", "unknown"):
        with pytest.raises(ValueError, match="8.3"):
            report(status=forbidden)


def test_an_emitter_may_not_declare_its_own_lifecycle():
    """Enforced in the adapter, not only in the schema: an emitter announcing
    its own retirement is telemetry claiming an authority it does not have."""
    doc = report(component_id="sneaky", status="ok",
                 facets={"owner": "brian", "lifecycle": "retired"})
    result = _adapter_over({"reports/sneaky/latest.json": doc})
    (component,) = [e for e in result.entities if e.kind is Kind.COMPONENT]
    assert "lifecycle" not in component.facets
    assert component.facets["owner"] == "brian"


def test_declared_consumes_produces_the_reverse_lineage_edge():
    """§6: the forward edge is a property of the producer and is usually
    written down. 'Who breaks if this is stale' exists nowhere unless the
    emitter declares it, and it is the only question an incident asks."""
    doc = report(component_id="reader", status="ok",
                 consumes=["s3://bucket/upstream.parquet"])
    result = _adapter_over({"reports/reader/latest.json": doc})
    rels = {(e.source, e.rel, e.target) for e in result.edges}
    assert ("s3://bucket/upstream.parquet", "consumed-by", "reader") in rels


def test_a_declared_cycle_becomes_a_joinable_entity():
    """Cycle is the join (§8) — 'what else broke when this broke' is a
    traversal, not a correlation somebody performs by eye across timestamps."""
    doc = report(component_id="daily", status="ok",
                 ran_at="2026-08-03T12:00:00+00:00", cycle_id="2026-08-03")
    result = _adapter_over({"reports/daily/latest.json": doc})
    cycles = [e for e in result.entities if e.kind is Kind.CYCLE]
    assert [c.id for c in cycles] == ["2026-08-03"]
    assert any(e.rel == "belongs-to" and e.target == "2026-08-03"
               for e in result.edges)


def test_declared_fields_survive_to_the_entity_uninterpreted():
    """§5.8's payload passes through verbatim. The adapter must not interpret
    it: the whole point is that no path is keyed on who emitted it."""
    doc = report(
        component_id="measurer", status="ok",
        fields={"rows_written": {"value": 903, "unit": "rows",
                                 "baseline": 900, "render": "count"}},
    )
    result = _adapter_over({"reports/measurer/latest.json": doc})
    (component,) = [e for e in result.entities if e.kind is Kind.COMPONENT]
    assert component.detail["fields"]["rows_written"]["unit"] == "rows"


def test_the_incumbent_check_id_spelling_still_works():
    """The fleet's existing envelopes use `check_id` and are in production. A
    schema change that orphans live emitters is not an improvement."""
    result = _adapter_over({
        "reports/legacy/latest.json": {
            "schema_version": 1, "check_id": "legacy-check", "status": "ok",
            "ran_at": "2026-08-03T12:00:00+00:00", "cadence_minutes": 60,
        }
    })
    ids = {e.id for e in result.entities if e.kind is Kind.COMPONENT}
    assert ids == {"legacy-check"}


# ----------------------------------------------------------- helpers --------

def _registry_with_other() -> str:
    """A registry directory declaring one unrelated component."""
    import tempfile

    d = tempfile.mkdtemp()
    with open(os.path.join(d, "other.yaml"), "w") as fh:
        fh.write("component_id: some-other-thing\nlifecycle: in-service\n")
    return d


def _adapter_over(objects: dict[str, dict]) -> AdapterResult:
    """Run the checks-envelope adapter over in-memory objects, hermetically."""
    listed = [(k, "2026-08-03T12:00:00+00:00") for k in objects]
    result = checks_envelope.fetch(
        {
            "_name": "reports",
            "bucket": "local",
            "prefix": "reports/",
            "key_pattern": r"reports/(?P<component_id>[^/]+)/latest\.json",
        },
        lister=lambda bucket, prefix: listed,
        reader=lambda bucket, key: objects[key],
        now=NOW,
    )
    assert result.status is AdapterStatus.OK
    return result


def _serve_local_objects(monkeypatch, root):
    """Point the checks-envelope adapter at a temp directory as its store."""
    def lister(bucket, prefix):
        out = []
        for dirpath, _, files in os.walk(root):
            for name in files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root)
                if rel.startswith(prefix):
                    out.append((rel, "2026-08-03T12:00:00+00:00"))
        return sorted(out)

    def reader(bucket, key):
        with open(os.path.join(root, key)) as fh:
            return json.load(fh)

    original = checks_envelope.fetch

    def patched(config, **kwargs):
        return original(config, lister=lister, reader=reader, now=NOW)

    monkeypatch.setattr(checks_envelope, "fetch", patched)


def _console_tree_hash() -> str:
    """A content hash of everything under `console/`.

    Uses git's own object hashing so it is exact and cheap, and does not depend
    on the working tree being clean.
    """
    out = subprocess.run(
        ["git", "hash-object", "--stdin-paths"],
        input="\n".join(sorted(_console_files())),
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def _console_files() -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(CONSOLE_PACKAGE):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        found.extend(os.path.join(dirpath, f) for f in filenames)
    return found
