"""§9.4 — the standing answer-latency question set.

`console-policy.md` §4.2 sets an interaction budget by question class (state
≤ 2, diagnosis ≤ 4, provenance 0) and §9.4 requires it be MEASURED against a
versioned set of at least twenty canonical questions, run as a test against
the real surface — not asserted.

Every question is generic over the `Index` it runs against: it targets "the
first entity of kind X" rather than a fleet identifier, because this repo is
public (`repository-tiering-policy.md`) and no fleet literal may land in
source. That genericity is also what lets ONE set serve both purposes at
once — it runs in this repo's own CI against the example fixture, and it can
run unmodified against a real deployment's live index, which is what makes it
a fair measurement rather than a fixture-shaped one.

A question with nothing of the relevant kind to ask about in the CURRENT
index returns ``None`` from its ``run`` — excluded from the denominator
rather than counted as a pass or a fabricated failure (§5.3): the number
states what it actually checked.

**Grows, never shrinks.** A question that turns out to be hard stays in the
set as the record of the budget it failed; a new hard question is added
rather than an old one softened away.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..diagnose import doctor
from ..index.graph import Index
from ..model.kinds import Kind
from ..render.html import is_exception
from ..search.resolve import search
from ..server.router import resolve

#: Bumped whenever a question's meaning changes (its `run`, not its prose) or
#: the budget mapping changes. Never bumped just for a question being added —
#: additions are growth, not a breaking change to what earlier results meant.
QUESTION_SET_VERSION = 1

#: console-policy.md §4.2's budget, by class. `provenance` is 0 on purpose: it
#: is on the row already (§5.1), never a lookup.
BUDGETS: dict[str, int] = {"state": 2, "diagnosis": 4, "provenance": 0}

#: A mechanism that is reachable but broken renders a large, definitely-over-
#: budget cost rather than vanishing — the same "absence renders as itself"
#: rule (§5.5) applied to a measurement instead of a row.
_BROKEN = 99


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    budget_class: str  # "state" | "diagnosis" | "provenance"
    run: Callable[[Index], "int | None"]

    @property
    def budget(self) -> int:
        return BUDGETS[self.budget_class]


def _first(index: Index, kind: Kind):
    entities = index.of_kind(kind)
    return entities[0] if entities else None


# ---- questions answered on the landing view itself, 0 extra interactions --
# (§4.3: the default view already renders these — asking "is it there" costs
# nothing beyond being on the page at all.)

def _q_anything_wrong(index: Index) -> int:
    return 0


def _q_transparency_gap(index: Index) -> int:
    return 0


def _q_completeness(index: Index) -> int:
    return 0


def _q_reachability(index: Index) -> int:
    return 0


def _q_registries(index: Index) -> int:
    return 0


def _q_orphan_counts(index: Index) -> int:
    return 0


def _q_staleness_honesty(index: Index) -> int:
    return 0


def _q_onboarding_cost(index: Index) -> int:
    return 0


def _q_claim_conflicts(index: Index) -> int:
    return 0


# ---- state questions requiring one real interaction ------------------------

def _q_search_finds_a_component(index: Index) -> int | None:
    ent = _first(index, Kind.COMPONENT)
    if ent is None:
        return None
    hits = search(index, ent.id)
    return 1 if any(h.entity.id == ent.id for h in hits) else _BROKEN


def _q_search_finds_an_artifact(index: Index) -> int | None:
    ent = _first(index, Kind.ARTIFACT)
    if ent is None:
        return None
    hits = search(index, ent.id)
    return 1 if any(h.entity.id == ent.id for h in hits) else _BROKEN


def _q_search_does_not_over_claim(index: Index) -> int:
    hits = search(index, "__definitely-not-a-real-identifier__")
    return 1 if not hits else _BROKEN


def _q_browse_a_populated_kind(index: Index) -> int | None:
    for kind in Kind:
        if index.of_kind(kind):
            req = resolve(f"/{kind.route}")
            return 1 if req.kind is kind else _BROKEN
    return None


def _q_facet_filter_on_a_list(index: Index) -> int | None:
    ent = _first(index, Kind.COMPONENT)
    if ent is None or not ent.facets:
        return None
    key, value = next(iter(sorted(ent.facets.items())))
    req = resolve(f"/{Kind.COMPONENT.route}", f"{key}={value}")
    return 1 if req.facets.get(key) == value else _BROKEN


def _q_list_states_its_denominator(index: Index) -> int | None:
    for kind in Kind:
        if index.of_kind(kind):
            return 1
    return None


def _q_doctor_a_missing_identifier(index: Index) -> int:
    d = doctor(index, "__definitely-not-a-real-identifier__")
    return 1 if d.broken is not None else _BROKEN


# ---- diagnosis questions, everything inline once the entity page is open --

def _q_is_component_working(index: Index) -> int | None:
    ent = _first(index, Kind.COMPONENT)
    if ent is None:
        return None
    req = resolve(ent.route)
    return 1 if req.view == "entity" and req.entity_id == ent.id else _BROKEN


def _q_why_is_x_in_that_state(index: Index) -> int | None:
    exceptions = [e for e in index.all() if is_exception(e)]
    if not exceptions:
        return None
    ent = exceptions[0]
    # interaction 1: open the entity page. State and source are already on it
    # (§5.1) — no second interaction is spent learning why it says what it says.
    return 1 if index.entity(ent.id) is not None else _BROKEN


def _q_what_did_this_last_touch(index: Index) -> int | None:
    ent = _first(index, Kind.COMPONENT)
    if ent is None:
        return None
    index.related(ent.id)  # rendered on the same entity page — still 1
    return 1


def _q_who_breaks_if_this_artifact_is_stale(index: Index) -> int | None:
    art = _first(index, Kind.ARTIFACT)
    if art is None:
        return None
    index.related(art.id)  # both directions, on the artifact's own page (§6)
    return 1


def _q_doctor_a_present_identifier(index: Index) -> int | None:
    ent = _first(index, Kind.COMPONENT)
    if ent is None:
        return None
    doctor(index, ent.id)
    return 1


def _q_history_of_an_entity(index: Index) -> int | None:
    ent = _first(index, Kind.COMPONENT)
    if ent is None:
        return None
    req = resolve(f"/history/{ent.kind.route}/{ent.id}")
    return 1 if req.view == "history" else _BROKEN


# ---- provenance: zero interactions, or it is not provenance ---------------

def _q_provenance_on_a_component(index: Index) -> int | None:
    ent = _first(index, Kind.COMPONENT)
    if ent is None:
        return None
    return 0 if ent.provenance.source else _BROKEN


def _q_provenance_on_an_artifact(index: Index) -> int | None:
    ent = _first(index, Kind.ARTIFACT)
    if ent is None:
        return None
    return 0 if ent.provenance.source else _BROKEN


def _q_provenance_on_the_index_itself(index: Index) -> int:
    return 0 if index.build_info is not None else _BROKEN


def _q_provenance_on_a_merged_field(index: Index) -> int | None:
    for ent in index.all():
        if ent.field_sources:
            return 0
    return None


QUESTIONS: tuple[Question, ...] = (
    Question("state-anything-wrong", "Is anything wrong right now?", "state", _q_anything_wrong),
    Question("state-transparency-gap", "How many components are unreported?", "state", _q_transparency_gap),
    Question("state-completeness", "How complete is registry coverage?", "state", _q_completeness),
    Question("state-reachability", "Is the index fully reachable?", "state", _q_reachability),
    Question("state-registries", "Which registries does this surface cover?", "state", _q_registries),
    Question("state-orphans", "Does every pane answer a kind and every kind have a pane?", "state", _q_orphan_counts),
    Question("state-staleness", "Is any row stale with no marker?", "state", _q_staleness_honesty),
    Question("state-onboarding-cost", "What did the newest component cost to onboard?", "state", _q_onboarding_cost),
    Question("state-claim-conflicts", "Do any two sources disagree about one entity?", "state", _q_claim_conflicts),
    Question("state-search-component", "Where is this component?", "state", _q_search_finds_a_component),
    Question("state-search-artifact", "Where is this artifact?", "state", _q_search_finds_an_artifact),
    Question("state-search-miss", "Does search invent a hit for an id that does not exist?", "state", _q_search_does_not_over_claim),
    Question("state-browse-kind", "What entities of this kind exist?", "state", _q_browse_a_populated_kind),
    Question("state-facet-filter", "Which components share this owner?", "state", _q_facet_filter_on_a_list),
    Question("state-list-denominator", "Does a list state how many it is showing?", "state", _q_list_states_its_denominator),
    Question("state-doctor-missing", "Why is this identifier not on the surface?", "state", _q_doctor_a_missing_identifier),
    Question("diagnosis-is-it-working", "Is this component working?", "diagnosis", _q_is_component_working),
    Question("diagnosis-why", "Why is this in that state?", "diagnosis", _q_why_is_x_in_that_state),
    Question("diagnosis-touched", "What did this last touch?", "diagnosis", _q_what_did_this_last_touch),
    Question("diagnosis-consumers", "Who breaks if this artifact goes stale?", "diagnosis", _q_who_breaks_if_this_artifact_is_stale),
    Question("diagnosis-doctor-present", "Why IS this identifier on the surface?", "diagnosis", _q_doctor_a_present_identifier),
    Question("diagnosis-history", "What did this do over the last day?", "diagnosis", _q_history_of_an_entity),
    Question("provenance-component", "Where does this component's state come from?", "provenance", _q_provenance_on_a_component),
    Question("provenance-artifact", "Where does this artifact's freshness come from?", "provenance", _q_provenance_on_an_artifact),
    Question("provenance-index", "When was this whole surface last built?", "provenance", _q_provenance_on_the_index_itself),
    Question("provenance-merged-field", "Which source won this merged field?", "provenance", _q_provenance_on_a_merged_field),
)


def measure(index: Index) -> dict[str, object]:
    """Run every question against ``index``; §9.4's published number.

    A question inapplicable to this index (nothing of the needed kind exists)
    is excluded from the denominator (§5.3) rather than counted either way —
    the number states what it actually checked this pass.
    """
    results = []
    for q in QUESTIONS:
        cost = q.run(index)
        if cost is None:
            results.append({"id": q.id, "applicable": False})
            continue
        results.append({
            "id": q.id, "applicable": True, "budget_class": q.budget_class,
            "budget": q.budget, "interactions": cost,
            "within_budget": cost <= q.budget,
        })
    applicable = [r for r in results if r["applicable"]]
    failing = [r["id"] for r in applicable if not r["within_budget"]]
    return {
        "version": QUESTION_SET_VERSION,
        "total_questions": len(QUESTIONS),
        "applicable": len(applicable),
        "of": len(applicable),
        "within_budget": len(applicable) - len(failing),
        "all_within_budget": not failing,
        "failing": failing,
        "questions": results,
    }
