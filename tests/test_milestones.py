"""console-policy.md §4.4's milestone pane — declared predicates, evaluated.

The defect this pane exists for: a milestone whose exit criteria were five
measurable clauses was evaluated BY HAND, off five other surfaces, from a
tracker issue. The clauses were all facts the console already rendered; nothing
assembled them into the predicate, so "is this phase finished" was a research
task rather than a page.

What is asserted here, and why each one is the failure mode rather than the
feature:

- an UNREPORTED clause is **never met and never unmet**. A binding that could
  not be read must not be graded, in either direction — a clause bound to a
  §9 number that refused to render would otherwise compare `None`/`0` and pass.
- the JSON and the HTML answer the predicate **identically**, from one
  assembly (§3.8). Two renderings of one URL that grade a milestone differently
  is the exact divergence §3.8 exists to forbid.
- an unknown binding kind or comparator **fails the build**, naming the
  milestone and clause. Degrading at render time publishes a pane that silently
  answers a question nobody asked it.
- **no `milestones:` key renders nothing** — no empty pane, no `milestones: []`
  on the wire, and the §9.5 orphan count is unmoved.
"""
from __future__ import annotations

import pytest

from console.config import build_index
from console.index import milestones as M
from console.index.graph import Index
from console.model.entity import Entity, Provenance
from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
from console.model.kinds import Kind, State
from console.render import html as render_html
from console.render import json as render_json
from console.render.panes import orphan_counts
from console.server.router import resolve


# --------------------------------------------------------------- fixtures --

def _index_with(*entities: Entity) -> Index:
    idx = Index()
    idx.add_result(AdapterResult(
        name="fixture", status=AdapterStatus.OK,
        claim_class=ClaimClass.OBSERVATION,
        entities=entities,
    ))
    return idx


def _signal(cid: str, cycles: int) -> Entity:
    return Entity(
        kind=Kind.SIGNAL, id=cid, state="reported",
        provenance=Provenance("fixture-streaks", "2026-08-27T00:00:00Z",
                              "https://example.invalid/streak"),
        detail={"fields": {
            "consecutive_cycles": {"value": cycles, "unit": "cycles",
                                   "render": "count"},
        }},
    )


def _verdict(cid: str, verdict: str) -> Entity:
    return Entity(
        kind=Kind.COMPONENT, id=cid, state=State.HEALTHY,
        provenance=Provenance("fixture-verdict", "2026-08-27T01:00:00Z",
                              "https://example.invalid/verdict"),
        detail={"fields": {
            "correctness_verdict": {"value": verdict, "render": "text"},
        }},
    )


#: One met clause, one unmet clause, one clause bound to nothing at all.
DECLARATION = [{
    "id": "example-exit",
    "question": "Has the example milestone exited, and which clause is holding it?",
    "tracker": "https://example.invalid/tracker/1",
    "clauses": [
        {"id": "c-met", "label": "the streak is long enough",
         "entity": "sig-alpha", "field": "consecutive_cycles",
         "op": ">=", "target": 4},
        {"id": "c-unmet", "label": "the verdict passes",
         "entity": "comp-verdict", "field": "correctness_verdict",
         "op": "==", "target": "PASS"},
        {"id": "c-unreported", "label": "a signal that has not landed yet",
         "entity": "sig-not-yet-implemented", "field": "consecutive_weeks",
         "op": ">=", "target": 4},
    ],
}]


def _evaluated(index: Index) -> list[dict]:
    M.attach(index, M.parse(DECLARATION))
    numbers = render_json.numbers(
        index, [], index.conflicts(), index.transparency_gap())
    return M.evaluate(index, numbers)


@pytest.fixture
def index() -> Index:
    return _index_with(_signal("sig-alpha", 6), _verdict("comp-verdict", "UNKNOWN"))


# ----------------------------------------------------- the three outcomes --

def test_a_met_an_unmet_and_an_unreported_clause_are_three_different_answers(index):
    [milestone] = _evaluated(index)
    statuses = {c["id"]: c["status"] for c in milestone["clauses"]}
    assert statuses == {
        "c-met": M.MET, "c-unmet": M.UNMET, "c-unreported": M.UNREPORTED,
    }


def test_an_unreported_clause_counts_toward_neither_met_nor_the_roll_up(index):
    """The whole point. An unread clause is not a pass and is not a failure —
    it is stated separately, and it holds the milestone."""
    [milestone] = _evaluated(index)
    assert milestone["met"] == 1
    assert milestone["of"] == 3
    assert milestone["unreported"] == 1
    assert milestone["holding"] == ["c-unmet", "c-unreported"]


def test_an_unreported_clause_says_why_and_never_renders_blank(index):
    [milestone] = _evaluated(index)
    clause = next(c for c in milestone["clauses"] if c["id"] == "c-unreported")
    [term] = clause["terms"]
    assert term["value"] is None
    assert "sig-not-yet-implemented" in term["reason"]
    assert clause["reason"]


def test_a_bound_fact_carries_its_own_as_of_source_and_evidence(index):
    """§5.1's four fields, per term — not the index's freshness standing in for
    the fact's."""
    [milestone] = _evaluated(index)
    [term] = next(c for c in milestone["clauses"] if c["id"] == "c-met")["terms"]
    assert term["as_of"] == "2026-08-27T00:00:00Z"
    assert term["source"] == "fixture-streaks"
    assert term["evidence"] == "/signal/sig-alpha"


def test_a_number_binding_is_stamped_with_the_index_build_time(index):
    """A §9 number is computed from this build and is exactly as old as it is."""
    import dataclasses
    index.build_info = dataclasses.replace(
        index.build_info, built_at="2026-08-27T02:00:00Z")
    M.attach(index, M.parse([{
        "id": "m", "question": "q?",
        "clauses": [{"id": "gap", "number": "transparency_gap", "path": "count",
                     "op": "==", "target": 0}],
    }]))
    numbers = render_json.numbers(
        index, [], index.conflicts(), index.transparency_gap())
    [milestone] = M.evaluate(index, numbers)
    [term] = milestone["clauses"][0]["terms"]
    assert term["as_of"] == "2026-08-27T02:00:00Z"
    assert term["evidence"] == "/"
    assert term["status"] == M.MET


def test_a_clause_bound_to_a_number_that_refused_to_render_is_unreported():
    """`requires:` — §9.6 publishes `computable: false` rather than a count over
    an unestablished population. Without the precondition the clause would read
    `None == 0` and, with a naive comparator, could pass."""
    idx = _index_with(_signal("sig-alpha", 1))
    M.attach(idx, M.parse([{
        "id": "m", "question": "q?",
        "clauses": [{
            "id": "honesty", "number": "staleness_honesty", "path": "count",
            "op": "==", "target": 0,
            "requires": {"path": "computable", "equals": True},
        }],
    }]))
    numbers = render_json.numbers(idx, [], idx.conflicts(), idx.transparency_gap())
    assert numbers["staleness_honesty"]["computable"] is False  # premise
    [milestone] = M.evaluate(idx, numbers)
    clause = milestone["clauses"][0]
    assert clause["status"] == M.UNREPORTED
    assert milestone["met"] == 0


def test_an_all_of_clause_is_unreported_when_any_one_binding_cannot_be_read():
    """Not `1 of 2 terms met`. A clause is a single predicate, and a predicate
    with an unreadable conjunct has no truth value."""
    idx = _index_with(_signal("sig-alpha", 9))
    M.attach(idx, M.parse([{
        "id": "m", "question": "q?",
        "clauses": [{"id": "both", "all_of": [
            {"entity": "sig-alpha", "field": "consecutive_cycles",
             "op": ">=", "target": 4},
            {"entity": "sig-beta", "field": "consecutive_cycles",
             "op": ">=", "target": 4},
        ]}],
    }]))
    numbers = render_json.numbers(idx, [], idx.conflicts(), idx.transparency_gap())
    [milestone] = M.evaluate(idx, numbers)
    assert milestone["clauses"][0]["status"] == M.UNREPORTED
    assert [t["status"] for t in milestone["clauses"][0]["terms"]] == [
        M.MET, M.UNREPORTED]


def test_an_ordering_over_a_non_number_is_unreported_not_unmet():
    """A config defect renders as one. Grading it False would report a fleet
    finding for a mistake in the declaration."""
    idx = _index_with(_verdict("comp-verdict", "PASS"))
    M.attach(idx, M.parse([{
        "id": "m", "question": "q?",
        "clauses": [{"id": "bad", "entity": "comp-verdict",
                     "field": "correctness_verdict", "op": ">=", "target": 4}],
    }]))
    numbers = render_json.numbers(idx, [], idx.conflicts(), idx.transparency_gap())
    [milestone] = M.evaluate(idx, numbers)
    assert milestone["clauses"][0]["status"] == M.UNREPORTED


def test_a_field_present_with_no_value_is_unreported_not_unequal():
    idx = _index_with(Entity(
        kind=Kind.COMPONENT, id="comp-verdict", state=State.HEALTHY,
        provenance=Provenance("fixture", "2026-08-27T00:00:00Z"),
        detail={"fields": {"correctness_verdict": {"value": None,
                                                   "render": "text"}}},
    ))
    M.attach(idx, M.parse([{
        "id": "m", "question": "q?",
        "clauses": [{"id": "v", "entity": "comp-verdict",
                     "field": "correctness_verdict", "op": "==",
                     "target": "PASS"}],
    }]))
    numbers = render_json.numbers(idx, [], idx.conflicts(), idx.transparency_gap())
    [milestone] = M.evaluate(idx, numbers)
    assert milestone["clauses"][0]["status"] == M.UNREPORTED


# ------------------------------------------------------------- both views --

def test_the_json_and_the_html_grade_the_predicate_identically(index):
    """§3.8: one query, two renderings. A milestone that reads MET on the page
    and UNMET on the wire is the divergence this policy exists to forbid."""
    M.attach(index, M.parse(DECLARATION))
    payload = render_json.payload(index, resolve("/", ""))
    page = render_html.landing_page(index)
    [milestone] = payload["milestones"]

    assert f'{milestone["met"]} of {milestone["of"]} clauses met' in page
    for clause in milestone["clauses"]:
        assert clause["id"] in page
        assert clause["label"] in page
    # The question sentence is rendered ON the pane (§4.4), not merely declared.
    assert milestone["question"] in page
    assert milestone["tracker"] in page
    assert "UNREPORTED" in page


def test_the_milestone_pane_renders_above_the_exception_table():
    idx = _index_with(
        _signal("sig-alpha", 6), _verdict("comp-verdict", "UNKNOWN"),
        Entity(kind=Kind.COMPONENT, id="comp-broken", state=State.FAILED,
               provenance=Provenance("fixture", "2026-08-27T00:00:00Z")),
    )
    M.attach(idx, M.parse(DECLARATION))
    page = render_html.landing_page(idx)
    assert page.index("milestone: example-exit") < page.index("comp-broken")
    assert page.index("milestone: example-exit") < page.index("waiting on Brian")


def test_no_milestones_declared_renders_nothing_at_all(index):
    """No pane, no empty state, and no key on the wire. A deployment that
    declares no milestone is not a deployment whose milestone is unknown."""
    payload = render_json.payload(index, resolve("/", ""))
    assert "milestones" not in payload
    assert "milestone:" not in render_html.landing_page(index)


def test_the_pane_is_registered_and_the_orphan_count_is_unchanged():
    """§4.4/§9.5. The pane is cross-cutting — a milestone's clauses bind across
    kinds and to the §9 numbers, which belong to no kind — so it adds no kind
    orphan and is not one itself."""
    counts = orphan_counts()
    assert counts["pane_orphans"]["count"] == 0
    assert counts["kind_orphans"]["count"] == 0
    from console.render.panes import PANES
    pane = next(p for p in PANES if p.name == "milestones")
    assert pane.kind is None
    assert pane.question == render_html._MILESTONE_PANE_QUESTION


# ------------------------------------------------------ build-time refusal --

@pytest.mark.parametrize("declaration,expected", [
    pytest.param(
        [{"id": "m", "question": "q?", "clauses": [
            {"id": "c", "table": "rows", "op": "==", "target": 0}]}],
        "exactly one of ['entity', 'number']",
        id="unknown-binding-kind",
    ),
    pytest.param(
        [{"id": "m", "question": "q?", "clauses": [
            {"id": "c", "number": "transparency_gap", "path": "count",
             "op": "~=", "target": 0}]}],
        "comparator '~=' is outside the closed vocabulary",
        id="unknown-comparator",
    ),
    pytest.param(
        [{"id": "m", "question": "q?", "clauses": [
            {"id": "c", "number": "transparency_gap", "op": "==", "target": 0}]}],
        "requires `path`",
        id="binding-with-no-selector",
    ),
    pytest.param(
        [{"id": "m", "question": "q?", "clauses": [
            {"id": "c", "number": "transparency_gap", "path": "count",
             "op": "=="}]}],
        "`target` is required",
        id="no-target",
    ),
    pytest.param(
        [{"id": "m", "question": "q?", "clauses": []}],
        "declares no clauses",
        id="no-clauses",
    ),
    pytest.param(
        [{"id": "m", "clauses": [
            {"id": "c", "number": "transparency_gap", "path": "count",
             "op": "==", "target": 0}]}],
        "`question` is required",
        id="no-question",
    ),
    pytest.param(
        [{"id": "m", "question": "q?", "clauses": [
            {"id": "c", "all_of": [
                {"entity": "e", "field": "f", "op": "==", "target": 1}],
             "requires": {"equals": True}}]}],
        "must name its own binding",
        id="requires-with-nothing-to-inherit",
    ),
])
def test_a_declaration_this_build_cannot_evaluate_fails_the_build(declaration,
                                                                 expected):
    """`parse` runs inside `build_index`, which is what CI's `console index
    --config config.example.yaml` step executes — so this is caught on the PR
    that introduces it, not on the surface a week later."""
    with pytest.raises(M.MilestoneConfigError) as excinfo:
        M.parse(declaration)
    assert expected in str(excinfo.value)
    # Always locatable: the message names the milestone, and the clause where
    # there is one. The assembled config.yaml has no memory of which fragment
    # contributed which block, so the ids are the locator.
    assert "'m'" in str(excinfo.value)


def test_the_build_itself_refuses_a_bad_declaration():
    with pytest.raises(M.MilestoneConfigError):
        build_index({"milestones": [{"id": "m", "question": "q?", "clauses": [
            {"id": "c", "number": "transparency_gap", "path": "count",
             "op": "≈", "target": 0}]}]})


def test_a_milestone_declared_twice_is_a_namespace_collision():
    with pytest.raises(M.MilestoneConfigError, match="declared twice"):
        M.parse([
            {"id": "m", "question": "q?", "clauses": [
                {"id": "c", "number": "transparency_gap", "path": "count",
                 "op": "==", "target": 0}]},
            {"id": "m", "question": "q?", "clauses": [
                {"id": "c", "number": "transparency_gap", "path": "count",
                 "op": "==", "target": 0}]},
        ])


def test_a_build_with_no_milestones_attaches_none():
    assert M.declared(build_index({})) == ()


def test_the_example_config_declares_a_parseable_milestone():
    """The committed template is the fixture CI builds against — a broken
    example would fail the build gate, and a template with no milestone block
    would leave the whole feature undocumented at the only place a deployment
    looks."""
    import yaml

    with open("config.example.yaml") as fh:
        example = yaml.safe_load(fh)
    parsed = M.parse(example["milestones"])
    assert parsed and all(m.clauses for m in parsed)


def test_every_clause_status_has_a_stylesheet_selector():
    """§5.7 — an unstyled row is a row whose status is carried by text alone in
    a table that styles every other row, which reads as "nothing special here"
    exactly where a clause is holding a milestone."""
    from pathlib import Path

    css = (Path(__file__).resolve().parent.parent
           / "console/static/styles.css").read_text()
    for status in (M.MET, M.UNMET, M.UNREPORTED):
        assert f".milestone-{status}" in css
