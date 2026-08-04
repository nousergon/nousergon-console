"""The descriptor carries its own source bindings (§2.6, §2.7) — CN-2.6/CN-2.7.

`test_a_component_in_a_place_no_adapter_points_at_onboards_for_free` **is** the
clause. It is the case the previous implementation could not serve: onboarding
was free only for components lucky enough to land under a prefix the console's
own `config.yaml` already named. Free **by coincidence**, not by design.

The defect, measured 2026-08-03: the source binding lived in the *adapter's*
config — `bucket` + `prefix` + `key_pattern` — which is **per-source, not
per-component**. A registry row carried `component_id`, `lifecycle` and facets
and **no source bindings at all**.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone

import pytest

from console.config import build_index
from console.drivers import DRIVERS, KNOWN_DRIVERS, resolve_bindings
from console.drivers.base import Cost
from console.model.descriptor import DescriptorError, parse
from console.model.kinds import Kind, State

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
CONSOLE_PACKAGE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "console"
)


def _registry(tmp_path, **rows):
    d = tmp_path / "registry.d"
    d.mkdir(exist_ok=True)
    import yaml

    for cid, row in rows.items():
        (d / f"{cid}.yaml").write_text(yaml.safe_dump({"component_id": cid, **row}))
    return {"adapter": "yaml-directory", "path": str(d), "id_field": "component_id"}


def _config(tmp_path, ctx=None, **rows):
    return {
        "registry": _registry(tmp_path, **rows),
        "_driver_context": {"now": NOW, **(ctx or {})},
    }


# ------------------------------------------------------------ the clause ---

def test_a_component_in_a_place_no_adapter_points_at_onboards_for_free(tmp_path):
    """One file. No adapter entry, no prefix added to config.yaml, no console
    edit — and the artifact lives in a bucket nothing else in this fleet uses.

    The content-hash guard is what makes the claim mean anything: without it,
    "onboarding is free" is a sentence a test would happily agree with while an
    implementer quietly added a special case.
    """
    before = _console_tree_hash()

    config = _config(
        tmp_path,
        ctx={"object_stat": lambda key: "2026-08-03T11:30:00+00:00"},
        **{"my-nightly-job": {
            "owner": "brian",
            "lifecycle": "in-service",
            "artifacts": {
                "driver": "object-store",
                "key": "s3://a-bucket-no-adapter-has-ever-heard-of/out.parquet",
                "cadence_minutes": 1440,
            },
            "consumes": ["s3://upstream/in.parquet"],
        }},
    )
    index = build_index(config)

    component = index.entity("my-nightly-job")
    assert component is not None and component.kind is Kind.COMPONENT
    artifact = index.entity("s3://a-bucket-no-adapter-has-ever-heard-of/out.parquet")
    assert artifact is not None and artifact.state == "fresh"

    # Reachable by relation, in both directions, from the descriptor alone.
    rels = {(e.rel, e.target) for e in index.related("my-nightly-job")}
    assert ("produces", artifact.id) in rels
    assert ("consumes", "s3://upstream/in.parquet") in rels

    assert _console_tree_hash() == before, (
        "onboarding required a change under console/ — §2.6 is not met and "
        "§9.8's target of zero is missed on the first component"
    )


def test_the_binding_is_in_the_descriptor_not_in_console_config(tmp_path):
    """The actual defect, asserted directly: nothing about this component's
    location appears in the console's configuration."""
    config = _config(
        tmp_path,
        ctx={"object_stat": lambda key: "2026-08-03T11:30:00+00:00"},
        **{"c": {"artifacts": {"driver": "object-store",
                               "key": "s3://b/k", "cadence_minutes": 60}}},
    )
    serialised = json.dumps({k: v for k, v in config.items()
                             if k != "_driver_context"}, default=str)
    assert "s3://b/k" not in serialised
    assert build_index(config).entity("s3://b/k") is not None


# -------------------------------------------------------- parsing rules ----

def test_an_unknown_driver_fails_the_build_loudly(tmp_path):
    """§2.7. Rendering the component as merely absent would make a typo
    indistinguishable from a component that is genuinely gone — §8.3's
    RETIRED/ABSENT collapse arriving through the configuration layer."""
    with pytest.raises(DescriptorError, match="log-souce"):
        parse({"metrics": {"driver": "log-souce", "field": "x"}}, "c",
              known_drivers=KNOWN_DRIVERS)


def test_the_error_names_the_component_the_key_and_the_known_set():
    try:
        parse({"metrics": {"driver": "nope"}}, "my-comp",
              known_drivers=KNOWN_DRIVERS)
    except DescriptorError as exc:
        text = str(exc)
        assert "my-comp" in text and "metrics" in text and "object-store" in text
    else:
        pytest.fail("expected DescriptorError")


def test_a_binding_with_no_driver_is_rejected():
    """A binding naming no shape is a source nobody can read, so nothing would
    be read and nothing would say so."""
    with pytest.raises(DescriptorError, match="no `driver`"):
        parse({"artifacts": {"key": "s3://b/k"}}, "c")


def test_one_mapping_and_a_list_are_both_accepted():
    """A descriptor format that fails on a defensible spelling is a descriptor
    format people work around."""
    single = parse({"artifacts": {"driver": "object-store", "key": "a"}}, "c")
    listed = parse({"artifacts": [{"driver": "object-store", "key": "a"}]}, "c")
    assert len(single.bindings) == len(listed.bindings) == 1


def test_lineage_is_not_a_binding():
    """Nothing is read to satisfy `produces`/`consumes` — the component is
    stating its own edges (§6), not pointing at a source."""
    d = parse({"produces": ["a"], "consumes": ["b"]}, "c")
    assert d.bindings == ()
    assert d.produces == ("a",) and d.consumes == ("b",)


# --------------------------------------------------------- driver rules ----

#: Literal shapes that name an INSTANCE rather than a source shape (§2.7).
#:
#: Deliberately narrow, and narrowed twice while being written — which is the
#: useful part. The first draft flagged any kebab-case string and fired on
#: `"no-cadence-declared"`, a legitimate member of a closed state vocabulary.
#: The second flagged the bare scheme token `"s3://"`, which a driver uses to
#: PARSE a URI — knowing the scheme it reads is exactly what a driver is for.
#: A check that cries wolf on correct code earns an exemption list, and an
#: exemption list is how a check stops checking. So the pattern requires
#: something AFTER the scheme: a bucket, an account, a function name.
INSTANCE_SHAPED = re.compile(
    r"^(?:s3://\S|arn:aws:[a-z0-9-]+:\S|/aws/lambda/\S"
    r"|https?://[a-z0-9.-]*(?:amazonaws|nousergon))"
)


def test_no_driver_source_names_an_instance():
    """§2.7's lint. A driver that could name a bucket, group, ARN or component
    is an adapter written for one component wearing a driver's name — and the
    special case is always defensible on the day it is added, which is why the
    check is mechanical rather than a review convention.

    Scanned by AST over **non-docstring** string constants. A regex over raw
    lines flagged the drivers' own usage examples, which are documentation and
    the most useful thing in the file — a lint that punishes documenting the
    format is a lint that removes the documentation.
    """
    import ast

    offenders = []
    drivers_dir = os.path.join(CONSOLE_PACKAGE, "drivers")
    for fname in sorted(os.listdir(drivers_dir)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(drivers_dir, fname)
        tree = ast.parse(open(path).read())
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings
                    and INSTANCE_SHAPED.match(node.value)):
                offenders.append(f"{fname}:{node.lineno}: {node.value!r}")
    assert offenders == [], f"driver source names an instance (§2.7): {offenders}"


def test_the_instance_lint_actually_catches_one():
    """A lint nobody has seen fail is a lint nobody knows works — and this one
    was rewritten after its first version fired on correct code."""
    assert INSTANCE_SHAPED.match("s3://some-bucket/key")
    assert INSTANCE_SHAPED.match("arn:aws:states:us-east-1:1:stateMachine:x")
    assert INSTANCE_SHAPED.match("/aws/lambda/my-function")
    # …and does not fire on a closed-vocabulary token, a driver shape name, or
    # the bare SCHEME a driver uses to parse a URI. Knowing the scheme it reads
    # is what a driver is; knowing which bucket is what it must never be.
    assert not INSTANCE_SHAPED.match("no-cadence-declared")
    assert not INSTANCE_SHAPED.match("object-store")
    assert not INSTANCE_SHAPED.match("s3://")
    assert not INSTANCE_SHAPED.match("arn:aws:")


def test_every_driver_declares_a_cost():
    """§2.7. Reading on the refresh pass is only sufficient if the cost is
    KNOWN — a driver that scans billed bytes on every rebuild is a different
    proposition from one that stats an object, and the difference must be
    visible before somebody points fifty descriptors at it."""
    for shape, module in DRIVERS.items():
        assert isinstance(module.cost, Cost), shape
        assert module.name == shape
        assert module.kinds


def test_one_components_broken_binding_does_not_blank_the_others(tmp_path):
    """A driver marked FAILED renders ALL its entities UNREPORTED. Doing that
    because one component of fifty has a bad key would blank forty-nine that
    are fine — an availability failure manufactured out of a typo."""
    def stat(key):
        if "broken" in key:
            raise RuntimeError("no such bucket")
        return "2026-08-03T11:30:00+00:00"

    config = _config(
        tmp_path, ctx={"object_stat": stat},
        **{
            "good": {"artifacts": {"driver": "object-store",
                                   "key": "s3://ok/k", "cadence_minutes": 60}},
            "bad": {"artifacts": {"driver": "object-store",
                                  "key": "s3://broken/k", "cadence_minutes": 60}},
        },
    )
    index = build_index(config)
    assert index.entity("s3://ok/k").state == "fresh"
    assert index.entity("s3://broken/k") is None  # its own binding, not others'


def test_a_failed_binding_is_named_by_doctor(tmp_path):
    """The entire reason bindings are named individually. "The component is not
    on the surface" is not actionable; "its artifacts binding failed: no such
    bucket" is."""
    from console.diagnose import doctor

    config = _config(
        tmp_path,
        ctx={"object_stat": lambda k: (_ for _ in ()).throw(RuntimeError("no such bucket"))},
        **{"c": {"artifacts": {"driver": "object-store", "key": "s3://x/k",
                               "cadence_minutes": 60}}},
    )
    d = doctor(build_index(config), "c")
    binding_steps = [s for s in d.steps if s.name.startswith("binding ")]
    assert binding_steps and "no such bucket" in binding_steps[0].detail


# ------------------------------------------------ freshness and absence ----

@pytest.mark.parametrize("stamp, cadence, expected", [
    ("2026-08-03T11:30:00+00:00", 60, "fresh"),
    ("2026-08-01T00:00:00+00:00", 60, "stale"),
    (None, 60, "absent"),
    ("2026-08-03T11:30:00+00:00", None, "no-cadence-declared"),
    ("not-a-date", 60, "unreadable"),
])
def test_the_not_computable_cases_stay_distinct_facts(tmp_path, stamp, cadence,
                                                      expected):
    """§5.5. No stamp, no declared cadence and an unparseable stamp are three
    different findings with three different fixes; collapsing them loses the
    fix."""
    spec = {"driver": "object-store", "key": "s3://b/k"}
    if cadence:
        spec["cadence_minutes"] = cadence
    config = _config(tmp_path, ctx={"object_stat": lambda k: stamp},
                     **{"c": {"artifacts": spec}})
    assert build_index(config).entity("s3://b/k").state == expected


# --------------------------------------------------- the emission binding --

def test_emission_is_now_one_binding_kind_not_the_path(tmp_path):
    """§2.6: for a module with nowhere natural to put a number. It still works,
    and it is no longer what onboarding means."""
    envelope = {
        "schema_version": 1, "status": "ok", "cadence_minutes": 60,
        "ran_at": "2026-08-03T11:30:00+00:00", "summary": "did the thing",
        "fields": {"rows": {"value": 903, "unit": "rows", "render": "count"}},
        "consumes": ["s3://upstream/in.parquet"],
    }
    config = _config(
        tmp_path, ctx={"document_reader": lambda key: envelope},
        **{"c": {"metrics": {"driver": "emitted-envelope",
                             "key": "s3://b/reports/c/latest.json"}}},
    )
    index = build_index(config)
    ent = index.entity("c")
    assert ent.state is State.HEALTHY
    assert ent.detail["fields"]["rows"]["unit"] == "rows"
    assert any(e.rel == "consumes" for e in index.related("c"))


def test_a_declared_but_never_written_envelope_is_a_named_finding(tmp_path):
    from console.diagnose import doctor

    def reader(key):
        raise FileNotFoundError(key)

    config = _config(tmp_path, ctx={"document_reader": reader},
                     **{"c": {"metrics": {"driver": "emitted-envelope",
                                          "key": "s3://b/none.json"}}})
    d = doctor(build_index(config), "c")
    assert any("never written" in s.detail for s in d.steps if not s.ok)


def test_a_half_written_envelope_says_so_rather_than_guessing(tmp_path):
    config = _config(tmp_path, ctx={"document_reader": lambda k: "{not json"},
                     **{"c": {"metrics": {"driver": "emitted-envelope",
                                          "key": "s3://b/k.json"}}})
    index = build_index(config)
    failures = [f for a in index.build_info.adapters for f in a.unavailable]
    assert any("did not parse" in f for f in failures)


# ------------------------------------------------------------- schema ------

def test_the_descriptor_schema_is_published_and_only_requires_an_id():
    path = os.path.join(CONSOLE_PACKAGE, "schemas",
                        "component_descriptor.schema.json")
    schema = json.load(open(path))
    assert schema["required"] == ["component_id"]
    binding = schema["$defs"]["binding"]
    assert binding["required"] == ["driver"]
    # The render vocabulary is the same closed set §5.8 uses. Two spellings of
    # one closed set is how a closed set stops being one.
    from console.model.fields import Render

    assert set(binding["properties"]["render"]["enum"]) == {r.value for r in Render}


def _console_tree_hash() -> str:
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
