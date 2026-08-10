"""§4.3's two remaining elements — the decision queue and the completeness
ratio (`nousergon-console-I5`, narrowed by `alpha-engine-config-I6492`).

Measured 2026-08-03 (`console-policy.md` §13): `render/html.py::landing_page`
already led with what is not HEALTHY and published the transparency-gap
count. Missing were "what is waiting on Brian" (the decision queue) and
§9.1's completeness ratio. This file covers both: the `Index` query methods
that compute them, and their rendering on the landing page and its JSON
representation (§3.8).
"""
from __future__ import annotations

from console.index.graph import Index
from console.model.entity import Entity
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render.html import landing_page
from console.render.json import payload as json_payload
from console.server.router import resolve
from tests.fixtures import prov


def _decision(id_: str, labels: list[str], state: str = "open") -> Entity:
    return Entity(kind=Kind.DECISION, id=id_, state=state,
                  provenance=prov("repos"), detail={"labels": labels})


def _registry_result(*ids: str) -> AdapterResult:
    return AdapterResult(
        name="registry", status=AdapterStatus.OK, claim_class=ClaimClass.DECLARATION,
        entities=tuple(
            Entity(kind=Kind.COMPONENT, id=i, state=State.UNREPORTED,
                   provenance=prov("registry"))
            for i in ids
        ),
    )


# ----------------------------------------------------------- decision queue

def test_decision_queue_includes_gate_decision_and_triage_session_only():
    idx = Index()
    idx.add_result(AdapterResult(
        name="repos", status=AdapterStatus.OK,
        entities=(
            _decision("repo-I1", ["gate:decision"]),
            _decision("repo-I2", ["triage:session"]),
            _decision("repo-I3", ["P1"]),  # ordinary backlog — not queued
            _decision("repo-I4", []),
        ),
    ))
    assert {e.id for e in idx.decision_queue()} == {"repo-I1", "repo-I2"}


def test_decision_queue_excludes_non_decision_kinds():
    """A Decision Queue label on an Incident does not enqueue it — Kind alone
    decides eligibility, matching §2.1's closed entity model."""
    idx = Index()
    idx.add_result(AdapterResult(
        name="repos", status=AdapterStatus.OK,
        entities=(Entity(kind=Kind.INCIDENT, id="repo-I9", state="open",
                         provenance=prov("repos"),
                         detail={"labels": ["gate:decision"]}),),
    ))
    assert idx.decision_queue() == []


def test_decision_queue_empty_when_no_decisions_at_all():
    assert Index().decision_queue() == []


def test_landing_page_renders_the_queued_decision_and_not_the_backlog_one():
    idx = Index()
    idx.add_result(AdapterResult(
        name="repos", status=AdapterStatus.OK,
        entities=(_decision("repo-I1", ["gate:decision"]),
                  _decision("repo-I3", ["P1"])),
    ))
    page = landing_page(idx)
    assert "repo-I1" in page
    assert "repo-I3" not in page


def test_landing_page_renders_empty_queue_as_itself_not_blank():
    """§5.5 — absence renders as itself, never as nothing: an empty decision
    queue is a rendered fact (nothing is waiting on Brian), not a missing
    section."""
    page = landing_page(Index())
    assert "waiting on brian" in page.lower()
    assert "no entities" in page.lower()


# ------------------------------------------------------- completeness ratio

def test_population_completeness_all_registry_rows_render():
    idx = Index()
    idx.add_result(_registry_result("comp-alpha", "comp-beta"))
    assert idx.population_completeness() == {
        "rendered": 2, "of": 2, "ratio": 1.0, "unregistered": 0,
    }


def test_population_completeness_counts_unregistered_separately_from_of():
    """observability-policy.md §8.4: two numbers, never blended. A discovered
    component with no matching declaration is UNREGISTERED and does not
    inflate the registry-rows denominator — `of` is what the registry
    declared, not what a substrate enumeration also found."""
    idx = Index()
    idx.add_result(_registry_result("comp-alpha"))
    idx.add_result(AdapterResult(
        name="discovery", status=AdapterStatus.OK, claim_class=ClaimClass.DISCOVERY,
        entities=(Entity(kind=Kind.COMPONENT, id="comp-ghost",
                         state=State.UNREPORTED, provenance=prov("discovery")),),
    ))
    c = idx.population_completeness()
    assert c["of"] == 1
    assert c["rendered"] == 1
    assert c["unregistered"] == 1


def test_population_completeness_no_registry_is_null_not_a_fabricated_1_0():
    """`config.example.yaml`'s documented behaviour: no registry means the
    ratio is unknown, never asserted as complete."""
    idx = Index()
    idx.add_result(AdapterResult(
        name="telemetry", status=AdapterStatus.OK,
        entities=(Entity(kind=Kind.COMPONENT, id="comp-x", state=State.HEALTHY,
                         provenance=prov("telemetry")),),
    ))
    c = idx.population_completeness()
    assert c["ratio"] is None
    assert c["of"] is None
    assert c["rendered"] == 0


def test_population_completeness_empty_index_is_null_not_zero():
    assert Index().population_completeness()["ratio"] is None


def test_landing_page_renders_the_completeness_ratio():
    idx = Index()
    idx.add_result(_registry_result("comp-alpha", "comp-beta"))
    page = landing_page(idx)
    assert "2 / 2" in page
    assert "100%" in page


def test_landing_page_renders_unknown_completeness_honestly_not_as_100_percent():
    page = landing_page(Index())
    assert "unknown" in page.lower()
    assert "100%" not in page


# ---------------------------------------------------------------- JSON §3.8

def test_json_landing_carries_the_same_decision_queue_and_completeness():
    idx = Index()
    idx.add_result(_registry_result("comp-alpha"))
    idx.add_result(AdapterResult(
        name="repos", status=AdapterStatus.OK,
        entities=(_decision("repo-I1", ["gate:decision"]),
                  _decision("repo-I2", ["P1"])),
    ))
    doc = json_payload(idx, resolve("/"))
    assert {e["id"] for e in doc["decision_queue"]} == {"repo-I1"}
    assert doc["numbers"]["population_completeness"] == {
        "rendered": 1, "of": 1, "ratio": 1.0, "unregistered": 0,
    }
