"""§9.4 answer latency (console-policy.md §4.2, §9.4).

A versioned, ≥20-question standing set, run as a test against the real
surface and measured in interaction counts against §4.2's budget: state ≤ 2,
diagnosis ≤ 4, provenance 0. Each question is generic over the index it runs
against (no fleet identifier — this repo is public) so the SAME set runs
against the fixture here and against a real deployment's live index.
"""
from __future__ import annotations

from console.index.graph import Index
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.qa.questions import BUDGETS, QUESTIONS, measure
from tests.fixtures import fixture_graph


def _rich_index() -> Index:
    """A fixture with at least one entity of every kind the question set
    probes, plus a facet and an exception, so every question is applicable."""
    from console.model.entity import Edge, Entity, Provenance
    from console.model.kinds import Kind, State

    entities, edges = fixture_graph()
    broken = Entity(kind=Kind.COMPONENT, id="comp-broken", state=State.FAILED,
                    provenance=Provenance("checks"), facets={"owner": "brian"})
    entities = list(entities) + [broken]
    idx = Index()
    idx.add_result(AdapterResult(
        name="fixture", status=AdapterStatus.OK, claim_class=ClaimClass.OBSERVATION,
        entities=tuple(entities), edges=tuple(edges),
    ))
    return idx


def test_the_question_set_has_at_least_twenty_questions():
    assert len(QUESTIONS) >= 20


def test_the_question_set_is_versioned():
    from console.qa.questions import QUESTION_SET_VERSION

    assert isinstance(QUESTION_SET_VERSION, int) and QUESTION_SET_VERSION >= 1


def test_every_question_declares_a_real_budget_class():
    for q in QUESTIONS:
        assert q.budget_class in BUDGETS


def test_the_question_set_runs_green_against_a_real_surface():
    """The closes-when clause: the set exists, is versioned, has ≥20
    questions, and runs green — every APPLICABLE question inside its budget."""
    result = measure(_rich_index())
    assert result["applicable"] >= 10  # the rich fixture exercises most of it
    assert result["all_within_budget"] is True, result["failing"]


def test_state_questions_are_bound_by_two_interactions():
    idx = _rich_index()
    for q in QUESTIONS:
        if q.budget_class != "state":
            continue
        cost = q.run(idx)
        if cost is None:
            continue
        assert cost <= 2, q.id


def test_provenance_questions_cost_zero_interactions():
    idx = _rich_index()
    for q in QUESTIONS:
        if q.budget_class != "provenance":
            continue
        cost = q.run(idx)
        if cost is None:
            continue
        assert cost == 0, q.id


def test_a_question_inapplicable_to_an_empty_index_is_excluded_not_failed():
    """Questions that need a real entity (search, browse, doctor-present,
    provenance-on-a-component, …) are excluded on an empty index — questions
    already answered on the landing view itself (is anything wrong, the §9
    numbers) remain applicable with 0 entities, because the landing view
    renders them regardless (§4.3)."""
    result = measure(Index())
    by_id = {r["id"]: r for r in result["questions"]}
    assert by_id["state-search-component"]["applicable"] is False
    assert by_id["diagnosis-is-it-working"]["applicable"] is False
    assert by_id["provenance-component"]["applicable"] is False
    assert by_id["state-anything-wrong"]["applicable"] is True
    assert result["of"] == result["applicable"]


def test_a_broken_search_lowers_the_search_questions_specifically(monkeypatch):
    """Mirrors §9.3's own test shape: breaking one mechanism must show up in
    the number that measures it, not get absorbed by the others."""
    from console.qa import questions as qmod

    idx = _rich_index()
    before = measure(idx)
    monkeypatch.setattr(qmod, "search", lambda index, query: [])
    after = measure(idx)
    assert after["all_within_budget"] is False
    assert any("search" in fid for fid in after["failing"])
    # Non-search questions must be unaffected.
    before_by_id = {r["id"]: r for r in before["questions"] if r["applicable"]}
    after_by_id = {r["id"]: r for r in after["questions"] if r["applicable"]}
    for qid, row in after_by_id.items():
        if "search" in qid:
            continue
        assert row["interactions"] == before_by_id[qid]["interactions"], qid
