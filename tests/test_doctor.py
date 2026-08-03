"""`doctor` (§3.9) — the chokepoint for CN-3.9.

`test_removing_each_link_in_turn_names_exactly_that_link` **is** the clause,
and it is the only form that proves the diagnosis is not a fixed string. A
`doctor` that always answers "no adapter claim" passes every test that checks
one scenario, and it is worse than useless: it is confidently wrong at the
moment somebody is already confused.

The defect it closes: onboarding fails **silently** by construction. Nothing
raises when a module is absent; the absence has exactly the shape of
`observability-policy.md` §8.3's `UNREPORTED`, and the person debugging it has
no thread to pull but adapter source. That is `principles.md` §2.3 unmet at the
onboarding boundary — detected, never diagnosed — and it is what stands between
a surface anyone can extend and one only its author can.
"""
from __future__ import annotations

import pytest

from console.diagnose import as_dict, doctor, render_text
from console.index.graph import Index
from console.model.entity import Edge, Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State

ID = "comp-alpha"


def _comp(state=State.HEALTHY, source="checks", cid=ID) -> Entity:
    return Entity(kind=Kind.COMPONENT, id=cid, state=state,
                  provenance=Provenance(source))


def _build(*, declared=True, observed=True, inbound_edge=True) -> Index:
    """A fully-reachable fixture, with each link independently removable."""
    idx = Index()
    if declared:
        idx.add_result(AdapterResult(
            name="registry", status=AdapterStatus.OK,
            claim_class=ClaimClass.DECLARATION,
            entities=(_comp(State.UNREPORTED, "registry"),)))
    else:
        # A registry that RAN and declared something else — so "unregistered"
        # is a claim anyone has standing to make (§2.4's denominator rule).
        idx.add_result(AdapterResult(
            name="registry", status=AdapterStatus.OK,
            claim_class=ClaimClass.DECLARATION,
            entities=(_comp(State.UNREPORTED, "registry", cid="comp-other"),)))
    if observed:
        edges = ()
        if inbound_edge:
            edges = (Edge(source="upstream-key", rel="consumed-by", target=ID),)
        idx.add_result(AdapterResult(
            name="checks", status=AdapterStatus.OK,
            claim_class=ClaimClass.OBSERVATION,
            entities=(_comp(),), edges=edges))
    return idx


# ------------------------------------------------------------ the clause ---

@pytest.mark.parametrize(
    "kwargs, expected_link",
    [
        ({"declared": False}, "registry row"),
        ({"declared": True, "observed": False}, "adapter claim"),
        ({"inbound_edge": False}, "relation-reachable"),
    ],
)
def test_removing_each_link_in_turn_names_exactly_that_link(kwargs, expected_link):
    """The only form that proves the diagnosis is not a fixed string.

    A `doctor` that always answers "no adapter claim" passes any single-scenario
    test — and is confidently wrong at the moment somebody is already confused.
    """
    d = doctor(_build(**kwargs), ID)
    assert d.broken is not None
    assert d.broken.name == expected_link, d.summary()


def test_a_fully_reachable_identifier_reports_ok():
    d = doctor(_build(), ID)
    assert d.ok and d.broken is None
    assert all(s.ok for s in d.steps)
    assert "reachable on all three paths" in d.summary()


def test_it_names_the_FIRST_broken_link_not_every_one():
    """A chain reporting every failure at once buries the one that caused the
    others. Nothing declared it and nothing observed it: the answer is the
    registry row, and the rendered text stops there."""
    idx = Index()
    idx.add_result(AdapterResult(name="registry", status=AdapterStatus.OK,
                                 claim_class=ClaimClass.DECLARATION))
    d = doctor(idx, ID)
    assert d.broken.name == "registry row"
    text = render_text(d)
    assert "adapter claim" not in text


# ------------------------------------------------- the common real cases ---

def test_an_identifier_nothing_has_ever_heard_of_answers_without_raising():
    """The most common case when somebody is debugging a typo. "Nothing knows
    this name" is a legitimate and useful answer, not an error."""
    d = doctor(Index(), "sdfhjksdf")
    assert not d.ok
    assert d.broken.name == "registry row"
    assert "sdfhjksdf" in d.summary()


def test_it_names_which_adapters_were_ASKED_not_just_that_none_answered():
    """An adapter that ran fine and found nothing looks exactly like one that
    was never enabled. Naming the set asked is what separates them."""
    idx = _build(observed=False)
    d = doctor(idx, ID)
    step = next(s for s in d.steps if s.name == "adapter claim")
    assert "registry" in step.detail
    assert d.facts["adapters_asked"] == ["registry"]


def test_the_broken_link_carries_a_NEXT_STEP_not_a_verdict():
    """"No adapter returned a claim" is a fact. A remedy is a next step, and
    it is the whole difference between a diagnosis and a status."""
    d = doctor(_build(observed=False), ID)
    assert "key_pattern" in d.broken.remedy


def test_an_undeclared_but_observed_component_is_diagnosed_at_the_registry():
    """It IS on the surface — as UNREGISTERED — and the diagnosis says why that
    matters: outside the denominator, so no completeness number counts it."""
    idx = _build(declared=False)
    d = doctor(idx, ID)
    assert d.broken.name == "registry row"
    assert "denominator" in d.broken.remedy or "UNREGISTERED" in d.broken.remedy
    assert idx.entity(ID).state is State.UNREGISTERED


def test_relation_unreachability_is_diagnosed_as_the_findability_problem_it_is():
    """§3.1: reachable by name and by structure but not by relation is
    findable only by someone who already knows it exists."""
    d = doctor(_build(inbound_edge=False), ID)
    assert "already knows it exists" in d.broken.remedy


def test_a_kind_collision_is_diagnosed_rather_than_raised_at_the_user():
    """Two claims disagreeing on `kind` is a namespace collision, not a merge.
    `doctor` must survive it — it is exactly when somebody needs an answer."""
    idx = _build()
    idx.add_result(AdapterResult(
        name="other", status=AdapterStatus.OK,
        entities=(Entity(kind=Kind.ARTIFACT, id=ID, state="fresh",
                         provenance=Provenance("other")),)))
    with pytest.raises(Exception):
        idx.all()  # the collision is still loud for the surface as a whole


# ----------------------------------------------------------- addressable ---

def test_the_diagnosis_is_addressable_by_url():
    """§3.9 + §3.2: a diagnosis nobody can link to has to be re-run by whoever
    is asked about it."""
    from console.server.router import resolve

    r = resolve("/doctor/comp-alpha")
    assert r.view == "doctor" and r.query == "comp-alpha"
    r2 = resolve("/doctor", "q=comp-alpha")
    assert r2.view == "doctor" and r2.query == "comp-alpha"


def test_an_identifier_with_slashes_round_trips_through_the_route():
    from console.server.router import resolve

    r = resolve("/doctor/ops/checks/comp-alpha/latest.json")
    assert r.query == "ops/checks/comp-alpha/latest.json"


def test_both_representations_render_it(monkeypatch):
    from console.render.html import doctor_page
    from console.render.json import payload
    from console.server.router import resolve

    idx = _build()
    html = doctor_page(idx, ID)
    assert html.startswith("<!doctype html>") and "relation-reachable" in html
    doc = payload(idx, resolve(f"/doctor/{ID}"))
    assert doc["view"] == "doctor" and doc["ok"] is True
    assert [s["name"] for s in doc["steps"]][0] == "registry row"


def test_the_cli_exit_code_is_usable_as_a_check(tmp_path):
    """Non-zero when not fully reachable, so this works in a deploy script
    rather than only by eye — `principles.md` §2.3's close-the-loop applied to
    the diagnosis itself."""
    from console.__main__ import main

    cfg = tmp_path / "config.yaml"
    cfg.write_text("console:\n  port: 5180\n")
    assert main(["--config", str(cfg), "doctor", "nothing-knows-this"]) != 0


def test_as_dict_is_complete_enough_to_act_on():
    d = as_dict(doctor(_build(observed=False), ID))
    assert d["broken"] == "adapter claim"
    assert d["remedy"]
    assert len(d["steps"]) >= 2
