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


# ============================================================================
# The clause journal — transitions, episodes, and one notification per episode.
#
# The defect these exist for, measured: two clauses of the Crucible phase-2
# exit predicate went MET -> UNMET inside three days (c3 `unregistered` 0 -> 3,
# c4 `staleness_honesty` 0 -> 2, live 2026-08-31T15:59Z) and NOTHING paged. The
# predicate was rendered on a page and read when somebody happened to look,
# which makes it a dashboard rather than a gate. `alpha-engine-config-I9083`
# recorded that same regression once, as a symptom; nothing was built that
# would catch the next one.
#
# What each of these asserts is a way the FIX fails, not a way the feature
# works:
#
# - keying on the BUILD instead of the episode. The index rebuilds every ~180s;
#   a per-build notification is ~480 pages a day for one regression. The fleet
#   has this exact mistake on record (an hourly timer keyed on the failing RUN:
#   5 CRITICALs and 5 RESOLVEDs for one condition).
# - a cold start paging for every clause that was already failing. A first
#   observation has no before and is not a regression.
# - announcing UNMET -> UNREPORTED as a recovery. That is a LOSS of
#   measurement, and calling it a fix is the lie this module exists to prevent.
# - a delivery failure eating the record. Recording is not delivery (§7.2a):
#   the transition is durable whether or not the pager worked, and the event is
#   retried rather than dropped or duplicated.
# - overwriting a journal that could not be read. That destroys the history AND
#   re-baselines every clause, silently suppressing the very episode this
#   catches.
# ============================================================================

import copy
import json as _json
import re


JOURNAL_BUCKET = "example-bucket"
JOURNAL_KEY = "console/milestones/example-exit/journal.json"


def _journalled(**overrides) -> list[dict]:
    """`DECLARATION` plus a journal block. Same clauses, so every assertion
    below is about the journal and never about the predicate."""
    decl = copy.deepcopy(DECLARATION)
    journal = {"bucket": JOURNAL_BUCKET, "key": JOURNAL_KEY,
               "notify": {"sns_topic_arn": "arn:aws:sns:us-east-1:0:example"}}
    journal.update(overrides)
    decl[0]["journal"] = journal
    return decl


class _Store:
    """An object store that round-trips through JSON, so a test can never pass
    on a mutation the real S3 writer would not have carried."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict] = {}
        self.writes = 0
        self.unreadable: str | None = None

    def read(self, bucket: str, obj: str):
        if self.unreadable:
            raise M.JournalUnreadable(self.unreadable)
        stored = self.objects.get((bucket, obj))
        return copy.deepcopy(stored) if stored is not None else None

    def write(self, bucket: str, obj: str, doc) -> None:
        self.writes += 1
        self.objects[(bucket, obj)] = _json.loads(_json.dumps(doc, default=str))

    @property
    def doc(self) -> dict:
        return self.objects[(JOURNAL_BUCKET, JOURNAL_KEY)]


class _Notifier:
    def __init__(self, fail_first: int = 0) -> None:
        self.events: list[dict] = []
        self.fail_first = fail_first
        self.attempts = 0

    def __call__(self, event) -> None:
        self.attempts += 1
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RuntimeError("transport down")
        self.events.append(copy.deepcopy(dict(event)))


def _run(index, store, notifier, now, declaration=None):
    M.attach(index, M.parse(declaration or _journalled()))
    numbers = render_json.numbers(
        index, [], index.conflicts(), index.transparency_gap())
    return M.journal(index, numbers=numbers, reader=store.read,
                     writer=store.write, notifier=notifier, now=now)


def _mixed(streak: int = 6, verdict: str = "UNKNOWN") -> Index:
    """c-met MET (streak >= 4), c-unmet UNMET, c-unreported UNREPORTED."""
    return _index_with(_signal("sig-alpha", streak), _verdict("comp-verdict", verdict))


# ------------------------------------------------- the milestone roll-up ----

def test_exit_state_is_holding_when_a_clause_was_measured_and_fails(index):
    [milestone] = _evaluated(index)
    assert milestone["exit_state"] == M.HOLDING
    assert milestone["exit_confirmed"] == 0


def test_exit_state_is_unreportable_when_nothing_failed_but_something_could_not_be_read():
    """A predicate the console could not READ is not a predicate that failed.
    Collapsing UNREPORTABLE into HOLDING would make an unreadable clause
    indistinguishable from a measured failing one — the distinction the whole
    module exists to hold, at the one level where a reader looks first."""
    idx = _index_with(_signal("sig-alpha", 6), _verdict("comp-verdict", "PASS"))
    declaration = copy.deepcopy(DECLARATION)
    M.attach(idx, M.parse(declaration))
    numbers = render_json.numbers(idx, [], idx.conflicts(),
                                  idx.transparency_gap())
    [milestone] = M.evaluate(idx, numbers)
    assert milestone["unreported"] == 1
    assert milestone["exit_state"] == M.UNREPORTABLE
    # And never confirmed: EXITED is reachable ONLY from all-MET, so no
    # unreadable clause can ever be counted as a clause that passed.
    assert milestone["exit_confirmed"] == 0


def test_exit_state_is_exited_only_when_every_clause_is_met():
    decl = copy.deepcopy(DECLARATION)
    decl[0]["clauses"] = [decl[0]["clauses"][0]]
    idx = _index_with(_signal("sig-alpha", 6))
    M.attach(idx, M.parse(decl))
    numbers = render_json.numbers(idx, [], idx.conflicts(),
                                  idx.transparency_gap())
    [milestone] = M.evaluate(idx, numbers)
    assert milestone["exit_state"] == M.EXITED
    assert milestone["exit_confirmed"] == 1


def test_the_verified_when_predicate_parses_under_the_fleet_gate_grammar(index):
    """`gate_data_sweep._build_ready_when_re` accepts exactly one content form:
    `field <name> >= <num>`. A predicate written against the STRING
    `exit_state` would be unconstructible and the sweep would escalate it to
    the Decision Queue rather than evaluate it — which is how sixteen of
    nineteen `gate:decision` PRs got there (gate-taxonomy-policy.md §5)."""
    M.attach(index, M.parse(_journalled()))
    numbers = render_json.numbers(index, [], index.conflicts(),
                                  index.transparency_gap())
    [milestone] = M.evaluate(index, numbers)
    grammar = re.compile(
        r"^[\s>*#-]*\**\s*Verified-when\**\s*[:：]\**\s*"
        r"(s3://\S+)\s+"
        r"(exists|>=\s*\d+\s*objects?|newer-than\s+\d{4}-\d{2}-\d{2}"
        r"|field\s+[\w.\-\[\]]+\s*>=\s*\d+(?:\.\d+)?)",
        re.IGNORECASE | re.MULTILINE,
    )
    assert grammar.search(f'Verified-when: {milestone["verified_when"]}')


# --------------------------------------------- the empty-population refusal --

def test_a_count_over_an_empty_population_refuses_rather_than_reading_met():
    """`{count: 0, of: 0}` REFUSES (alpha-engine-config-I9052). A fallback
    build renders every source `ok` over nothing at all, and a gap of `0 of 0`
    then reads as a perfect surface — which is exactly how a blanked index
    looked healthy for ten minutes on 2026-08-28. A clause graded MET off that
    would turn the outage into an exit criterion being met."""
    idx = Index()
    M.attach(idx, M.parse([{
        "id": "m", "question": "q?",
        "clauses": [{"id": "c", "number": "transparency_gap", "path": "count",
                     "op": "==", "target": 0}],
    }]))
    [milestone] = M.evaluate(idx, {"transparency_gap": {"count": 0, "of": 0}})
    clause = milestone["clauses"][0]
    assert clause["status"] == M.UNREPORTED
    assert "empty or uncomputable denominator" in clause["reason"]
    # And the same number over a real population still grades normally, so the
    # guard is a refusal on an empty denominator and not a blanket refusal.
    [ok] = M.evaluate(idx, {"transparency_gap": {"count": 0, "of": 42}})
    assert ok["clauses"][0]["status"] == M.MET


def test_a_denominator_that_could_not_be_computed_also_refuses():
    """§9.1 signals uncomputable with `of: None`. A clause bound to its
    `unregistered` member must not read `0 == 0` off that."""
    idx = Index()
    M.attach(idx, M.parse([{
        "id": "m", "question": "q?",
        "clauses": [{"id": "c", "number": "population_completeness",
                     "path": "unregistered", "op": "==", "target": 0}],
    }]))
    [milestone] = M.evaluate(
        idx, {"population_completeness": {"unregistered": 0, "of": None}})
    assert milestone["clauses"][0]["status"] == M.UNREPORTED


def test_the_guard_never_blocks_a_clause_reading_the_refusal_itself():
    """`requires: {path: computable, equals: true}` is how §9.6's refusal is
    made honest. If the empty-population guard fired on `computable` too, that
    precondition could never be evaluated and the mechanism would invert."""
    idx = Index()
    M.attach(idx, M.parse([{
        "id": "m", "question": "q?",
        "clauses": [{"id": "c", "number": "staleness_honesty", "path": "count",
                     "op": "==", "target": 0,
                     "requires": {"path": "computable", "equals": True}}],
    }]))
    [milestone] = M.evaluate(idx, {"staleness_honesty": {
        "computable": False, "count": 0, "of": 0}})
    clause = milestone["clauses"][0]
    assert clause["status"] == M.UNREPORTED
    assert "declined to state a value" in clause["reason"]


# ------------------------------------------------------ journal declaration --

def test_a_journal_with_nowhere_to_land_fails_the_build():
    with pytest.raises(M.MilestoneConfigError, match="both `bucket` and `key`"):
        M.parse(_journalled(bucket=""))


def test_a_journal_key_that_is_a_prefix_fails_the_build():
    with pytest.raises(M.MilestoneConfigError, match="one object, not a prefix"):
        M.parse([dict(DECLARATION[0], journal={
            "bucket": JOURNAL_BUCKET, "key": "console/milestones/"})])


def test_no_journal_block_persists_nothing_and_notifies_nothing(index):
    """The state of `config.example.yaml`, of the CI build gate, and of every
    console deployment that has not asked for a journal. A milestone pane must
    not become a thing that writes to somebody's bucket by existing."""
    store, notifier = _Store(), _Notifier()
    reports = _run(index, store, notifier, "2026-08-31T12:00:00Z",
                   declaration=DECLARATION)
    assert reports == []
    assert store.writes == 0 and notifier.events == []


# ------------------------------------------------------------ the baseline --

def test_the_first_build_records_a_baseline_and_pages_for_nothing():
    """A cold start has no before. Paging for every clause that was already
    failing before anyone was watching would make the first deploy the noisiest
    event in the system, and would train the reader to ignore it."""
    store, notifier = _Store(), _Notifier()
    [report] = _run(_mixed(), store, notifier, "2026-08-31T12:00:00Z")
    assert report["written"] is True
    assert notifier.events == []
    assert {t["clause"] for t in report["transitions"]} == {
        "c-met", "c-unmet", "c-unreported"}
    assert all(t["baseline"] for t in report["transitions"])
    # The already-failing clauses hold OPEN episodes, marked as baseline, so
    # their eventual recovery is not announced as a recovery from a page
    # nobody received.
    episodes = store.doc["episodes"]
    assert set(episodes) == {"c-unmet", "c-unreported"}
    assert all(e["baseline"] and not e["notified"] for e in episodes.values())


# --------------------------------------------- one notification per episode --

def test_a_clause_regressing_notifies_exactly_once_however_often_it_rebuilds():
    """THE assertion. The index rebuilds every ~180s; a notification keyed on
    the build is ~480 pages a day for one regression."""
    store, notifier = _Store(), _Notifier()
    _run(_mixed(streak=6), store, notifier, "2026-08-31T12:00:00Z")
    assert notifier.events == []

    _run(_mixed(streak=1), store, notifier, "2026-08-31T12:03:00Z")
    assert len(notifier.events) == 1
    [event] = notifier.events
    assert event["state"] == M.OPENED
    assert (event["from"], event["to"]) == (M.MET, M.UNMET)
    assert event["clause_id"] == "c-met"
    assert event["severity"] == "error"
    assert event["identity_key"] == "milestone:example-exit:clause:c-met"
    # The binding that MOVED, with before and after — a page saying only
    # "c-met is UNMET" sends the reader to another surface to find out why.
    assert event["moved"] == [{
        "binding": "entity:sig-alpha.consecutive_cycles",
        "from": 6, "to": 1, "target": 4, "op": ">="}]

    for minute in range(6, 60, 3):
        _run(_mixed(streak=1), store, notifier,
             f"2026-08-31T12:{minute:02d}:00Z")
    assert len(notifier.events) == 1, "one regression, one notification"


def test_an_unchanged_rebuild_writes_nothing_at_all():
    """Not merely "notifies nothing" — writes nothing. A journal rewritten 480
    times a day has an `updated_at` that means nothing, and its own age stops
    being usable as a liveness signal."""
    store, notifier = _Store(), _Notifier()
    _run(_mixed(), store, notifier, "2026-08-31T12:00:00Z")
    writes = store.writes
    for minute in (3, 6, 9, 12):
        [report] = _run(_mixed(), store, notifier,
                        f"2026-08-31T12:{minute:02d}:00Z")
        assert report["written"] is False
    assert store.writes == writes
    assert notifier.events == []


def test_the_heartbeat_rewrites_on_its_declared_cadence_without_notifying():
    """A journal rewritten only on change is indistinguishable, by age, from a
    journal nothing is writing any more. Absence of a signal is never health."""
    store, notifier = _Store(), _Notifier()
    _run(_mixed(), store, notifier, "2026-08-31T12:00:00Z")
    writes = store.writes
    _run(_mixed(), store, notifier, "2026-08-31T12:30:00Z")
    assert store.writes == writes, "inside the cadence: no write"
    _run(_mixed(), store, notifier, "2026-08-31T13:05:00Z")
    assert store.writes == writes + 1
    assert notifier.events == [], "a heartbeat is not a transition"


# ------------------------------------------------------- symmetric recovery --

def test_recovery_notifies_once_under_the_same_identity_key():
    """OB-7.2: recovery notifies symmetrically. The identity key is the same
    string for the page and its clear, and carries nothing about the build —
    no timestamp, no index generation — which is what makes the pair joinable
    by whatever is holding the open condition."""
    store, notifier = _Store(), _Notifier()
    _run(_mixed(streak=6), store, notifier, "2026-08-31T12:00:00Z")
    _run(_mixed(streak=1), store, notifier, "2026-08-31T12:03:00Z")
    [opened] = notifier.events

    _run(_mixed(streak=9), store, notifier, "2026-08-31T12:06:00Z")
    assert len(notifier.events) == 2
    cleared = notifier.events[1]
    assert cleared["state"] == M.CLEARED
    assert cleared["close_reason"] == M.RECOVERED
    assert cleared["identity_key"] == opened["identity_key"]
    assert cleared["severity"] == "info"
    assert "RESOLVED" in cleared["subject"]
    # The episode is closed, so a later rebuild cannot clear it twice.
    _run(_mixed(streak=9), store, notifier, "2026-08-31T12:09:00Z")
    assert len(notifier.events) == 2
    assert "c-met" not in store.doc["episodes"]


def test_a_clause_that_stops_being_readable_is_never_announced_as_recovered():
    """UNMET -> UNREPORTED is a LOSS of measurement, not a fix. Detection
    blindness outranks the defect it hides, so it is `error` and not `info`,
    and the clear that retires the previous page says SUPERSEDED."""
    store, notifier = _Store(), _Notifier()
    decl = copy.deepcopy(_journalled())
    decl[0]["clauses"] = [{"id": "c", "label": "the verdict passes",
                           "entity": "comp-verdict",
                           "field": "correctness_verdict",
                           "op": "==", "target": "PASS"}]
    _run(_index_with(_verdict("comp-verdict", "PASS")), store, notifier,
         "2026-08-31T12:00:00Z", declaration=decl)
    _run(_index_with(_verdict("comp-verdict", "FAIL")), store, notifier,
         "2026-08-31T12:03:00Z", declaration=decl)
    assert [e["state"] for e in notifier.events] == [M.OPENED]

    # The entity leaves the index entirely: the clause can no longer be read.
    _run(_index_with(), store, notifier, "2026-08-31T12:06:00Z",
         declaration=decl)
    states = [(e["state"], e.get("close_reason"), e["severity"])
              for e in notifier.events]
    assert states == [
        (M.OPENED, None, "error"),
        (M.CLEARED, M.SUPERSEDED, "info"),
        (M.OPENED, None, "error"),
    ]
    assert "SUPERSEDED" in notifier.events[1]["subject"]
    assert "did NOT recover" in notifier.events[1]["message"]
    assert notifier.events[2]["to"] == M.UNREPORTED


def test_a_baseline_failure_recovering_clears_nothing_it_never_paged_for():
    """A clear for a page nobody received is noise, and it is how a recovery
    feed stops meaning anything."""
    store, notifier = _Store(), _Notifier()
    _run(_mixed(streak=1), store, notifier, "2026-08-31T12:00:00Z")
    assert notifier.events == []
    _run(_mixed(streak=9), store, notifier, "2026-08-31T12:03:00Z")
    assert notifier.events == []


# ------------------------------------------ recording is not delivery (§7.2a) --

def test_a_failed_delivery_still_records_the_transition_and_retries_it_once():
    """Suppression is a delivery decision and never a recording one. The
    regression is durable whether or not the pager worked — and the event is
    retried rather than dropped, then delivered EXACTLY once in total."""
    store, notifier = _Store(), _Notifier(fail_first=1)
    _run(_mixed(streak=6), store, notifier, "2026-08-31T12:00:00Z")
    _run(_mixed(streak=1), store, notifier, "2026-08-31T12:03:00Z")

    assert notifier.events == [], "the transport was down"
    recorded = [t for t in store.doc["transitions"] if not t.get("baseline")]
    assert [(t["from"], t["to"]) for t in recorded] == [(M.MET, M.UNMET)]
    [pending] = store.doc["pending_notifications"]
    assert pending["attempts"] == 1 and "transport down" in pending["last_error"]

    _run(_mixed(streak=1), store, notifier, "2026-08-31T12:06:00Z")
    assert len(notifier.events) == 1
    assert store.doc["pending_notifications"] == []

    _run(_mixed(streak=1), store, notifier, "2026-08-31T12:09:00Z")
    assert len(notifier.events) == 1, "retry delivered it, once"


def test_an_unreadable_journal_refuses_rather_than_overwriting_the_history():
    """Overwriting it would destroy the recorded history AND re-baseline every
    clause — which silently suppresses the very episode this catches. Refusing
    loudly is the only safe move, and the refusal is rendered."""
    store, notifier = _Store(), _Notifier()
    _run(_mixed(streak=6), store, notifier, "2026-08-31T12:00:00Z")
    writes = store.writes
    store.unreadable = "s3://example-bucket/... is not valid JSON"

    [report] = _run(_mixed(streak=1), store, notifier, "2026-08-31T12:03:00Z")
    assert store.writes == writes, "nothing was overwritten"
    assert notifier.events == []
    assert "not valid JSON" in report["error"]


# ------------------------------------------------------------- the surface --

def test_the_journal_is_rendered_on_the_pane_and_served_on_the_wire():
    """§3.8. A transition is a fact the CONSOLE HOLDS — not something a reader
    reconstructs by remembering last week's number, which is how two clauses
    went MET -> UNMET in three days with nobody noticing (I9083)."""
    store, notifier = _Store(), _Notifier()
    idx = _mixed(streak=6)
    _run(idx, store, notifier, "2026-08-31T12:00:00Z")
    idx = _mixed(streak=1)
    reports = _run(idx, store, notifier, "2026-08-31T12:03:00Z")
    M.attach_journal_report(idx, reports)

    payload = render_json.payload(idx, resolve("/", ""))
    [recorded] = payload["milestone_journal"]
    assert recorded["milestone_id"] == "example-exit"
    assert any(t["clause"] == "c-met" and t["to"] == M.UNMET
               for t in recorded["recent"])

    page = render_html.landing_page(idx)
    assert "clause journal" in page
    assert "exit state: <strong>HOLDING</strong>" in page
    assert "field exit_confirmed &gt;= 1" in page
    assert "entity:sig-alpha.consecutive_cycles 6 → 1" in page


def test_a_journal_that_could_not_be_recorded_says_so_on_the_page():
    """A recorder failing silently is the defect this module exists to remove,
    so its own failure may not be silent either."""
    idx = _mixed()
    M.attach(idx, M.parse(_journalled()))
    M.attach_journal_report(idx, [{
        "milestone_id": "example-exit", "written": False,
        "error": "AccessDenied: the console cannot write its own journal"}])
    page = render_html.landing_page(idx)
    assert "clause journal UNREADABLE" in page
    assert "could regress unannounced" in page


def test_the_build_never_dies_because_the_journal_could_not_be_written():
    """One bad recorder must not blank a surface a dozen working sources are
    rendering (alpha-engine-config-I8778). The failure is recorded ON the
    index, which is louder than a stack trace in a log nobody reads."""
    idx = build_index({
        "milestones": _journalled(),
        "console": {"repo_root": "."},
    })
    [report] = M.journal_report(idx)
    # No credentials in the test environment: the default reader raises, and
    # the build carried on and said what happened.
    assert report["written"] is False
    assert report.get("error") or report.get("transitions") is not None
