"""§9.1 population completeness and §9.2 transparency-gap count.

console-policy.md §9.1/§9.2 defer to observability-policy.md §8.4 for the
definition: population completeness is registry rows RENDERED ÷ registry
rows, and separately the UNREGISTERED count; the transparency-gap count is
components in UNREPORTED. Both are computable against a fixture/example
registry now (nousergon-console#16) — the real fleet registry is tracked
separately (alpha-engine-config-I6115) — and §11's N/A-NOT-IMPL carve-out
does NOT cover either number, so an unreadable registry must render an
honest not-computable shape instead of the reserved placeholder token.

`Index.population_completeness()` itself landed in `nousergon-console-PR65`
(`nousergon-console-I5`, §4.3's decision-queue/completeness-ratio work) with
a 4-key shape (`rendered`/`of`/`ratio`/`unregistered`; `of`/`ratio` both
`None` signals "not computable"). This file extends that already-shipped
implementation to also degrade honestly when a SECOND, declared registry
fails to read — PR65's own `_saw_ok_declaration` check goes true the moment
ONE registry succeeds, which would otherwise silently report a ratio as if a
second, broken registry did not exist.
"""
from __future__ import annotations

from console.config import build_index
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State


def _write_registry(dirpath, components):
    import os
    import yaml

    for cid, extra in components.items():
        row = {"component_id": cid, "lifecycle": "in-service", "owner": "x", **extra}
        with open(os.path.join(dirpath, f"{cid}.yaml"), "w") as fh:
            yaml.safe_dump(row, fh)


def test_population_completeness_over_a_healthy_registry_is_1(tmp_path):
    reg = tmp_path / "registry.d"
    reg.mkdir()
    _write_registry(str(reg), {"comp-one": {}, "comp-two": {}})
    config = {"registry": {"adapter": "yaml-directory", "path": str(reg),
                           "id_field": "component_id"}}
    index = build_index(config)
    result = index.population_completeness()
    assert result["ratio"] == 1.0
    assert result["rendered"] == 2
    assert result["of"] == 2


def test_population_completeness_names_its_denominator_not_total_entities():
    """The defect this replaces: denominator was `len(entities)` — every kind,
    including entities a registry never declared. Here a non-component entity
    must not inflate the registry-row denominator."""
    idx = Index()
    idx.declare_registry("registry")
    idx.add_result(AdapterResult(
        name="registry", status=AdapterStatus.OK, claim_class=ClaimClass.DECLARATION,
        entities=(Entity(kind=Kind.COMPONENT, id="comp-one", state=State.UNREPORTED,
                         provenance=Provenance("registry")),),
    ))
    idx.record_registry_rows("registry", count=1, ok=True)
    idx.add_result(AdapterResult(
        name="checks", status=AdapterStatus.OK, claim_class=ClaimClass.OBSERVATION,
        entities=(Entity(kind=Kind.ARTIFACT, id="s3://b/k.json", state="fresh",
                         provenance=Provenance("checks")),),
    ))
    result = idx.population_completeness()
    assert result["of"] == 1  # ONE registry row, not two entities


def test_population_completeness_uncomputable_with_no_registry():
    idx = Index()
    result = idx.population_completeness()
    assert result["ratio"] is None
    assert result["of"] is None
    assert "N/A-NOT-IMPL" not in str(result)  # the reserved token is forbidden here (§11)


def test_population_completeness_uncomputable_when_a_second_registry_is_unreadable(tmp_path):
    reg = tmp_path / "registry.d"
    reg.mkdir()
    _write_registry(str(reg), {"comp-one": {}})
    config = {"registries": [
        {"name": "primary", "adapter": "yaml-directory", "path": str(reg),
         "id_field": "component_id"},
        {"name": "secondary", "adapter": "yaml-directory",
         "path": str(tmp_path / "does-not-exist"), "id_field": "component_id"},
    ]}
    index = build_index(config)
    result = index.population_completeness()
    # A second, unreadable registry must not let the ratio silently report 1.0
    # over just the registry that happened to work (§5.3 — no aggregate over
    # incomplete input). Without this PR's addition, `_saw_ok_declaration`
    # alone would have let this through: `primary` succeeded, so the old
    # guard passed, and the ratio would have been 1/1 as though `secondary`
    # were never configured at all.
    assert result["ratio"] is None
    assert result["of"] is None


def test_population_completeness_reports_unregistered_separately(tmp_path):
    """§8.4: "the first says the surface shows what it knows about; the second
    says how much it did not know about" — two numbers, never blended."""
    reg = tmp_path / "registry.d"
    reg.mkdir()
    _write_registry(str(reg), {"comp-one": {}})
    config = {"registry": {"adapter": "yaml-directory", "path": str(reg),
                           "id_field": "component_id"},
              "adapters": [{"name": "units", "kind": "local-units", "enabled": True,
                            "config": {"unit_prefixes": []}}]}
    index = build_index(config)
    # Inject a discovery claim for a component the registry never declared.
    index.add_result(AdapterResult(
        name="discovery", status=AdapterStatus.OK, claim_class=ClaimClass.DISCOVERY,
        entities=(Entity(kind=Kind.COMPONENT, id="comp-wild", state=State.UNREPORTED,
                         provenance=Provenance("discovery")),),
    ))
    result = index.population_completeness()
    assert result["unregistered"] == 1


def test_population_completeness_unregistered_excludes_run_kind_entities():
    """alpha-engine-config-I6970: a RUN entity that never matched its own
    component's registry row must not count toward `unregistered` — the
    checks-envelope adapter mints run ids as `f"{check_id}@{ran_at}"`
    (console/adapters/checks_envelope.py), which never equals the component
    id a registry row declares, even when that component IS registered. Left
    unscoped to Kind.COMPONENT, this inflated the live count 8.5x (15 of 17
    exceptions were runs of already-declared components, not registry gaps)."""
    idx = Index()
    idx.declare_registry("registry")
    idx.add_result(AdapterResult(
        name="registry", status=AdapterStatus.OK, claim_class=ClaimClass.DECLARATION,
        entities=(Entity(kind=Kind.COMPONENT, id="box_memory_headroom",
                         state=State.UNREPORTED, provenance=Provenance("registry")),),
    ))
    idx.record_registry_rows("registry", count=1, ok=True)
    # A run of the SAME component, minted with the check_id@ran_at id shape —
    # never declared under that exact id, only discovered/observed.
    idx.add_result(AdapterResult(
        name="checks", status=AdapterStatus.OK, claim_class=ClaimClass.DISCOVERY,
        entities=(Entity(kind=Kind.RUN,
                         id="box_memory_headroom@2026-08-12T14:48:52.988741+00:00",
                         state=State.UNREPORTED, provenance=Provenance("checks")),),
    ))
    result = idx.population_completeness()
    assert result["unregistered"] == 0


def test_transparency_gap_denominator_is_component_population_not_all_entities():
    idx = Index()
    idx.add_result(AdapterResult(
        name="registry", status=AdapterStatus.OK, claim_class=ClaimClass.DECLARATION,
        entities=(
            Entity(kind=Kind.COMPONENT, id="comp-reported", state=State.HEALTHY,
                  provenance=Provenance("registry")),
            Entity(kind=Kind.COMPONENT, id="comp-silent", state=State.UNREPORTED,
                  provenance=Provenance("registry")),
        ),
    ))
    idx.add_result(AdapterResult(
        name="checks", status=AdapterStatus.OK, claim_class=ClaimClass.OBSERVATION,
        entities=(Entity(kind=Kind.ARTIFACT, id="s3://b/k.json", state="fresh",
                         provenance=Provenance("checks")),),
    ))
    result = idx.transparency_gap()
    assert result == {"count": 1, "of": 2}  # 2 components, not 3 entities


def test_transparency_gap_is_zero_when_nothing_is_unreported():
    idx = Index()
    idx.add_result(AdapterResult(
        name="registry", status=AdapterStatus.OK, claim_class=ClaimClass.DECLARATION,
        entities=(Entity(kind=Kind.COMPONENT, id="comp-one", state=State.HEALTHY,
                         provenance=Provenance("registry")),),
    ))
    assert idx.transparency_gap() == {"count": 0, "of": 1}


# ---- §9.1 names the rows it counted (alpha-engine-config-I7107) -----------
#
# The count moved 0 -> 1 on the live surface between 19:45Z and 21:45Z on
# 2026-08-12 and could not be attributed: `population_completeness` published
# `{of, ratio, rendered, unregistered}` and nothing else, and an UNREGISTERED
# component is one line in a 100+ row exception table. Four probes over SSM
# failed to name it. Same defect `nousergon-console-PR86` fixed for §9.6.


def _index_with_one_unregistered(tmp_path):
    reg = tmp_path / "registry.d"
    reg.mkdir()
    _write_registry(str(reg), {"comp-one": {}})
    index = build_index({"registry": {"adapter": "yaml-directory", "path": str(reg),
                                      "id_field": "component_id"}})
    index.add_result(AdapterResult(
        name="discovery", status=AdapterStatus.OK, claim_class=ClaimClass.DISCOVERY,
        entities=(Entity(kind=Kind.COMPONENT, id="recovery_path_staleness",
                         state=State.UNREPORTED, provenance=Provenance("discovery")),),
    ))
    return index


def test_population_completeness_names_its_unregistered_members(tmp_path):
    result = _index_with_one_unregistered(tmp_path).population_completeness()
    assert result["unregistered"] == 1
    assert result["unregistered_ids"] == ["recovery_path_staleness"]


def test_population_completeness_member_lists_are_present_when_empty(tmp_path):
    """Always-present keys, empty when the count is 0. A key that appears only
    on failure makes every consumer write the absent-key branch, and one that
    skips it reads a healthy surface as a schema error."""
    reg = tmp_path / "registry.d"
    reg.mkdir()
    _write_registry(str(reg), {"comp-one": {}})
    result = build_index({"registry": {"adapter": "yaml-directory", "path": str(reg),
                                       "id_field": "component_id"}}).population_completeness()
    assert result["unregistered"] == 0
    assert result["unregistered_ids"] == []
    assert result["unrendered_ids"] == []


def test_population_completeness_names_members_on_the_uncomputable_branch():
    """`of`/`ratio` None is the uncomputable SIGNAL, not permission to drop the
    other count's evidence: an unreadable second registry is exactly when
    knowing WHICH rows are unregistered matters most."""
    idx = Index()
    idx.declare_registry("registry-a")
    idx.declare_registry("registry-b")  # declared, never reported -> unread
    idx.add_result(AdapterResult(
        name="registry-a", status=AdapterStatus.OK, claim_class=ClaimClass.DECLARATION,
        entities=(Entity(kind=Kind.COMPONENT, id="comp-one", state=State.UNREPORTED,
                         provenance=Provenance("registry-a")),),
    ))
    idx.record_registry_rows("registry-a", count=1, ok=True)
    idx.add_result(AdapterResult(
        name="discovery", status=AdapterStatus.OK, claim_class=ClaimClass.DISCOVERY,
        entities=(Entity(kind=Kind.COMPONENT, id="comp-wild", state=State.UNREPORTED,
                         provenance=Provenance("discovery")),),
    ))
    result = idx.population_completeness()
    assert result["of"] is None and result["ratio"] is None
    assert result["unregistered"] == 1
    assert result["unregistered_ids"] == ["comp-wild"]


def test_the_unregistered_members_are_navigable_by_state_url(tmp_path):
    """§3.1's structure path: enumerable is not navigable. `/component?state=
    UNREGISTERED` reaches the rows behind the count, in BOTH representations
    of the same URL (§3.8)."""
    from console.render import html as render_html
    from console.render import json as render_json
    from console.server.router import path_for_list, resolve

    index = _index_with_one_unregistered(tmp_path)
    url = path_for_list(Kind.COMPONENT, {"state": "UNREGISTERED"})
    assert url == "/component?state=UNREGISTERED"

    path, _, qs = url.partition("?")
    req = resolve(path, qs)
    doc = render_json.payload(index, req)
    assert [e["id"] for e in doc["entities"]] == ["recovery_path_staleness"]
    assert doc["filtered"] == 1

    page = render_html.list_page(index, req.kind, req.facets, req.page)
    assert "recovery_path_staleness" in page
    assert "comp-one" not in page  # the filter is applied, not decorative


def test_the_state_filter_is_case_insensitive(tmp_path):
    """An empty list reads as "nothing in this state", never as "you typed it
    wrong" (§5.4) — so casing must not be load-bearing."""
    from console.render import json as render_json
    from console.server.router import resolve

    index = _index_with_one_unregistered(tmp_path)
    doc = render_json.payload(index, resolve("/component", "state=unregistered"))
    assert [e["id"] for e in doc["entities"]] == ["recovery_path_staleness"]


def test_the_landing_page_links_the_unregistered_members(tmp_path):
    """§5.1's evidence field on a NUMBER: the count links to the rows."""
    from console.render import html as render_html

    page = render_html.landing_page(_index_with_one_unregistered(tmp_path))
    assert "/component?state=UNREGISTERED" in page
    assert '<a href="/component/recovery_path_staleness">' in page
