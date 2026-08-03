"""Claims merge by identifier (§2.5) — the chokepoint for CN-2.5.

The defect these guard against: `Index._add_entity` raised `NamespaceCollision`
on the **second** claim for one identifier. Two adapters describing one thing is
the normal case, so the fleet's own intended configuration — a registry
declaring a component plus a checks-envelope adapter observing it — crashed the
index. And `UNREGISTERED` and `ABSENT` were uncomputable by construction: both
require a declaration claim and a discovery claim about one identifier to
coexist, and the second one to arrive raised.

Each test below is one clause of §2.5 or one of `observability-policy.md` §8.3's
rules about which source may say what.
"""
from __future__ import annotations

import pytest

from console.index.graph import Index, NamespaceCollision
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State


def _prov(name: str, as_of: str | None = None) -> Provenance:
    return Provenance(source=name, as_of=as_of, evidence=f"https://x/{name}")


def _comp(state, source, as_of=None, facets=None, cid="comp-alpha") -> Entity:
    return Entity(
        kind=Kind.COMPONENT,
        id=cid,
        state=state,
        provenance=_prov(source, as_of),
        facets=facets or {},
    )


def _result(name, claim_class, *entities, status=AdapterStatus.OK) -> AdapterResult:
    return AdapterResult(
        name=name, status=status, claim_class=claim_class, entities=tuple(entities)
    )


def _index(*results) -> Index:
    idx = Index()
    for r in results:
        idx.add_result(r)
    return idx


# ---------------------------------------------------------------- the core --

def test_the_fleets_own_configuration_no_longer_crashes():
    """Registry declares + envelope observes. This raised before §2.5."""
    idx = _index(
        _result("registry", ClaimClass.DECLARATION,
                _comp(State.UNREPORTED, "registry", facets={"owner": "brian"})),
        _result("checks", ClaimClass.OBSERVATION,
                _comp(State.HEALTHY, "checks", as_of="2026-08-03T12:00:00Z")),
    )
    entities = idx.all()
    assert len(entities) == 1
    merged = entities[0]
    assert merged.state is State.HEALTHY
    assert merged.facets["owner"] == "brian"


def test_a_merged_row_names_a_source_per_field_not_the_last_adapter():
    """§5.1 stays truthful across a merge: three sources, named per field."""
    idx = _index(
        _result("registry", ClaimClass.DECLARATION,
                _comp(State.UNREPORTED, "registry", facets={"owner": "brian"})),
        _result("checks", ClaimClass.OBSERVATION,
                _comp(State.HEALTHY, "checks", as_of="2026-08-03T12:00:00Z")),
        _result("systemd", ClaimClass.DISCOVERY,
                _comp(State.HEALTHY, "systemd", facets={"substrate": "shared-box"})),
    )
    merged = idx.entity("comp-alpha")
    assert merged.source_of("state").source == "checks"
    assert merged.source_of("facets.owner").source == "registry"
    assert merged.source_of("facets.substrate").source == "systemd"


def test_merge_is_independent_of_adapter_order():
    """A surface that renders differently because two adapters raced is a
    surface nobody can reason about."""
    a = _result("registry", ClaimClass.DECLARATION, _comp(State.UNREPORTED, "registry"))
    b = _result("checks", ClaimClass.OBSERVATION, _comp(State.FAILED, "checks"))
    assert _index(a, b).entity("comp-alpha").state is _index(b, a).entity("comp-alpha").state


# ---------------------------------------------- the §8.3 lifecycle guard ----

def test_a_declared_lifecycle_survives_an_observation_that_says_otherwise():
    """§8.3: a decision and a defect are indistinguishable from telemetry alone,
    so the declaration is what tells them apart — and it must not be overridden
    by the very signal that cannot make the distinction."""
    idx = _index(
        _result("registry", ClaimClass.DECLARATION, _comp(State.DISABLED, "registry")),
        _result("checks", ClaimClass.OBSERVATION, _comp(State.MISSED, "checks")),
    )
    merged = idx.entity("comp-alpha")
    assert merged.state is State.DISABLED
    # The observation is not discarded — it is visible as a superseded claim,
    # because "it also did not run" is worth reading next to "it is off".
    assert any(f == "state" and v is State.MISSED for f, v, _ in merged.superseded)


def test_an_observation_may_not_invent_a_declared_lifecycle():
    """The other direction, and the more dangerous one: a defect rendered as a
    decision stops paging anybody. Only a declaration may say DISABLED."""
    idx = _index(
        _result("registry", ClaimClass.DECLARATION,
                _comp(State.UNREPORTED, "registry", cid="comp-other")),
        _result("systemd", ClaimClass.DISCOVERY, _comp(State.DISABLED, "systemd")),
    )
    merged = idx.entity("comp-alpha")
    assert merged.state is not State.DISABLED
    assert merged.state is State.UNREGISTERED  # discovered, never declared


def test_a_masked_unit_with_no_registry_row_is_not_silently_disabled():
    """systemd's `masked` is real information and it is not authority. With no
    declaration, the honest answer is loud, and the substrate's claim stays
    readable on the entity."""
    idx = _index(
        _result("registry", ClaimClass.DECLARATION, _comp(State.UNREPORTED, "registry")),
        _result("systemd", ClaimClass.DISCOVERY, _comp(State.DISABLED, "systemd")),
    )
    merged = idx.entity("comp-alpha")
    assert merged.state is State.UNREPORTED
    assert any(v is State.DISABLED for _, v, _ in merged.superseded)


# ------------------------------------- the two states merge made possible ---

def test_unregistered_is_computable_now():
    """Found running with no registry row (§8.3). Requires a declaration claim
    and a non-declaration claim to coexist, which is why it was uncomputable
    before the merge — the second one to arrive raised."""
    idx = _index(
        _result("registry", ClaimClass.DECLARATION,
                _comp(State.UNREPORTED, "registry", cid="comp-other")),
        _result("systemd", ClaimClass.DISCOVERY, _comp(State.HEALTHY, "systemd")),
    )
    assert idx.entity("comp-alpha").state is State.UNREGISTERED


def test_unregistered_needs_a_SUCCESSFUL_declaration_pass():
    """With no registry configured at all there is no denominator (§2.4), so
    "unregistered" is not a claim anyone has standing to make. Asserting it
    anyway would paint an entire registry-less surface red on a configuration
    choice — a verdict about the operator rather than about the fleet.

    Same shape as the ABSENT guard below: a state whose meaning IS absence may
    only be asserted by a check that actually looked."""
    idx = _index(
        _result("systemd", ClaimClass.DISCOVERY, _comp(State.HEALTHY, "systemd")),
    )
    assert idx.entity("comp-alpha").state is State.HEALTHY


def test_absent_needs_a_SUCCESSFUL_discovery_pass():
    """§8.3 forbids reading absence of evidence as evidence. A declared
    component is ABSENT only when a discovery adapter ran FINE and did not find
    it; with no successful discovery, its absence is unobserved, not
    established."""
    declared = _result("registry", ClaimClass.DECLARATION,
                       _comp(State.UNREPORTED, "registry"))

    # No discovery adapter ran at all → stays UNREPORTED, not ABSENT.
    assert _index(declared).entity("comp-alpha").state is State.UNREPORTED

    # A discovery adapter ran and FAILED → still not ABSENT.
    failed = _result("systemd", ClaimClass.DISCOVERY,
                     _comp(State.HEALTHY, "systemd", cid="comp-other"),
                     status=AdapterStatus.FAILED)
    assert _index(declared, failed).entity("comp-alpha").state is State.UNREPORTED

    # A discovery adapter ran fine and found something else → ABSENT.
    ok = _result("systemd", ClaimClass.DISCOVERY,
                 _comp(State.HEALTHY, "systemd", cid="comp-other"))
    assert _index(declared, ok).entity("comp-alpha").state is State.ABSENT


# ------------------------------------------------------- conflict, §9.9 -----

def test_equal_rank_disagreement_renders_rather_than_picking():
    """Two observations disagreeing is a fact about the fleet, not a rendering
    problem. It is DEGRADED with the field named — never resolved by arrival
    order, and never fatal."""
    idx = _index(
        _result("checks-a", ClaimClass.OBSERVATION, _comp(State.HEALTHY, "checks-a")),
        _result("checks-b", ClaimClass.OBSERVATION, _comp(State.FAILED, "checks-b")),
    )
    merged = idx.entity("comp-alpha")
    assert merged.state is State.DEGRADED
    assert "state" in merged.conflicts
    assert len(idx.conflicts()) == 1


def test_a_conflict_never_empties_or_truncates_the_surface():
    idx = _index(
        _result("a", ClaimClass.OBSERVATION, _comp(State.HEALTHY, "a")),
        _result("b", ClaimClass.OBSERVATION, _comp(State.FAILED, "b")),
        _result("c", ClaimClass.OBSERVATION, _comp(State.HEALTHY, "c", cid="comp-beta")),
    )
    assert {e.id for e in idx.all()} == {"comp-alpha", "comp-beta"}


# ------------------------------------------------- what still fails loud ----

def test_two_different_kinds_under_one_identifier_still_raise():
    """§3.6 is unchanged. `kind` disagreement is the one form of duplicate
    identifier no precedence rule can resolve honestly, so it stays a build
    error rather than becoming a merge."""
    idx = Index()
    idx.add_result(_result("a", ClaimClass.OBSERVATION, _comp(State.HEALTHY, "a")))
    idx.add_result(AdapterResult(
        name="b", status=AdapterStatus.OK, claim_class=ClaimClass.OBSERVATION,
        entities=(Entity(kind=Kind.ARTIFACT, id="comp-alpha", state="fresh",
                         provenance=_prov("b")),),
    ))
    with pytest.raises(NamespaceCollision, match="kind disagreement"):
        idx.all()


def test_a_failed_adapter_downgrades_its_own_claim_not_the_merged_entity():
    """§2.5: one unreachable source must not blank an entity three other
    sources reported on fine. Before the merge this was structurally
    impossible to get right — there was only ever one claim."""
    idx = _index(
        _result("checks", ClaimClass.OBSERVATION,
                _comp(State.HEALTHY, "checks", as_of="2026-08-03T12:00:00Z")),
        _result("dead", ClaimClass.OBSERVATION, _comp(State.HEALTHY, "dead"),
                status=AdapterStatus.FAILED),
    )
    assert idx.entity("comp-alpha").state is State.HEALTHY


# ------------------------------------------------------------ freshness -----

def test_merged_as_of_is_the_freshest_claim_not_the_highest_ranked():
    """A merged row's as-of is when the fact was last true according to SOME
    source (§5.1). Taking the declaration's would report a registry file's
    absent freshness over live telemetry's."""
    idx = _index(
        _result("registry", ClaimClass.DECLARATION, _comp(State.UNREPORTED, "registry")),
        _result("old", ClaimClass.OBSERVATION,
                _comp(State.HEALTHY, "old", as_of="2026-08-01T00:00:00Z")),
        _result("new", ClaimClass.OBSERVATION,
                _comp(State.HEALTHY, "new", as_of="2026-08-03T12:00:00Z")),
    )
    assert idx.entity("comp-alpha").provenance.as_of == "2026-08-03T12:00:00Z"


def test_every_adapter_declares_a_claim_class():
    """An adapter that never declares silently inherits OBSERVATION, which is
    the honest default but the wrong answer for a registry — and the failure is
    invisible, because a declaration ranked as an observation still merges."""
    from console.config import ADAPTERS

    for name, module in ADAPTERS.items():
        assert hasattr(module, "CLAIM_CLASS"), f"{name} declares no claim class"
        assert isinstance(module.CLAIM_CLASS, ClaimClass)
