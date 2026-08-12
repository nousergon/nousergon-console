"""console-policy.md §9 — all nine numbers, published on the console, in both
representations at the same URL (§3.8). nousergon-console#16.
"""
from __future__ import annotations

from console.config import build_index
from console.render import html as render_html
from console.render import json as render_json
from console.server.router import resolve

NUMBER_KEYS = (
    "population_completeness",   # §9.1 — no N/A-NOT-IMPL carve-out
    "transparency_gap",          # §9.2 — no N/A-NOT-IMPL carve-out
    "index_reachability",        # §9.3
    "answer_latency",            # §9.4
    "orphan_count",               # §9.5
    "staleness_honesty",         # §9.6
    "surface_liveness",          # §9.7 — computed from a declared external watcher
    "onboarding_cost",           # §9.8
    "claim_conflicts",           # §9.9
)


def _built_example_index():
    import os

    root = os.path.dirname(os.path.dirname(__file__))
    config = {
        "registry": {"adapter": "yaml-directory",
                     "path": os.path.join(root, "example", "registry.d"),
                     "id_field": "component_id"},
    }
    return build_index(config)


def test_all_nine_numbers_are_present_in_the_json_landing_payload():
    index = _built_example_index()
    doc = render_json.payload(index, resolve("/"))
    for key in NUMBER_KEYS:
        assert key in doc["numbers"], key


def test_every_number_states_its_denominator_inline():
    """§5.3: a roll-up that cannot state its denominator is not rendered."""
    index = _built_example_index()
    numbers = render_json.payload(index, resolve("/"))["numbers"]
    # Each number is either an {count,of} aggregate, or a structured dict that
    # itself carries an "of" (or nested "of"s) — never a bare number.
    assert numbers["transparency_gap"]["of"] >= 0
    assert numbers["claim_conflicts"]["of"] >= 0
    assert "of" in numbers["population_completeness"]
    assert "total" in numbers["index_reachability"]
    assert "of" in numbers["answer_latency"]
    assert "of" in numbers["staleness_honesty"]
    assert numbers["orphan_count"]["pane_orphans"]["of"] >= 0
    assert numbers["orphan_count"]["kind_orphans"]["of"] >= 0
    assert "of" in numbers["onboarding_cost"]


def test_9_1_and_9_2_never_render_the_na_not_impl_token():
    """§11's carve-out is explicit: N/A-NOT-IMPL is not available for §9.1 or
    §9.2, on an empty index where every other number legitimately falls back
    to it."""
    from console.index.graph import Index

    doc = render_json.payload(Index(), resolve("/"))
    numbers = doc["numbers"]
    assert numbers["population_completeness"].get("state") != "N/A-NOT-IMPL"
    assert numbers["transparency_gap"].get("state") != "N/A-NOT-IMPL"


class TestSurfaceLiveness:
    """§9.7 — is this surface up, according to something that is not it?

    Computed from a declared external watcher as of the console-exposure probe
    going live (`alpha-engine-config-I6491`). It was a hardcoded
    `N/A-NOT-IMPL` constant before that, correctly, because there was no
    watcher; leaving the constant once one existed would have been the console
    reporting a gap it no longer had.

    The console still never computes this from its own uptime — it renders an
    external watcher's verdict, which reached the index through an adapter like
    any other component. If the console is dark, nothing here renders at all,
    which is why the watcher's own alerting path and not this number is what
    reaches a human.
    """

    def test_no_watcher_declared_is_not_impl_and_says_what_would_fix_it(self):
        index = _built_example_index()
        n = render_json.payload(index, resolve("/"))["numbers"]["surface_liveness"]
        assert n["state"] == "N/A-NOT-IMPL"
        assert n["watcher"] is None
        assert "liveness_watcher" in n["reason"]

    def test_a_declared_but_missing_watcher_is_UNREPORTED_not_not_impl(self):
        """The distinction the number exists for. `N/A-NOT-IMPL` means nobody
        has claimed the job; a declared watcher absent from the index means it
        is not running, or nothing reads what it writes — a finding, and a
        worse state than never having declared one."""
        index = _built_example_index()
        index.set_liveness_watcher("a-watcher-nothing-produced")
        n = render_json.payload(index, resolve("/"))["numbers"]["surface_liveness"]
        assert n["state"] == "UNREPORTED"
        assert n["watcher"] == "a-watcher-nothing-produced"
        assert "not running" in n["reason"]

    def test_a_present_watcher_renders_its_state_as_of_and_evidence(self):
        """The row contract (§5.1) applies to this number like any other row:
        state, source, as-of, evidence link."""
        index = _built_example_index()
        watcher = next(iter(index.all()))
        index.set_liveness_watcher(watcher.id)
        n = render_json.payload(index, resolve("/"))["numbers"]["surface_liveness"]
        assert n["state"] == watcher.state.value
        assert n["watcher"] == watcher.id
        assert n["source"]
        assert n["evidence"].endswith(watcher.id)
        # `as_of` is PRESENT and may be None — a source with no freshness stamp
        # declares that absence rather than having a default invented for it
        # (`Provenance.as_of`). Absent-from-the-payload and None mean different
        # things, so the key is asserted rather than its truthiness.
        assert "as_of" in n

    def test_the_watcher_id_comes_from_configuration_not_from_console_source(self):
        """§2.3: which component watches this surface is a fleet fact. A
        default here would be a topology literal in a published repo."""
        import inspect

        from console.index import graph

        source = inspect.getsource(graph.Index.surface_liveness)
        for literal in ("nousergon-console", "alpha-engine", "console-exposure"):
            assert literal not in source

    def test_build_index_reads_the_declared_watcher_from_config(self):
        import os

        from console.config import build_index

        root = os.path.dirname(os.path.dirname(__file__))
        index = build_index({
            "registry": {"adapter": "yaml-directory",
                         "path": os.path.join(root, "example", "registry.d"),
                         "id_field": "component_id"},
            "console": {"liveness_watcher": "declared-in-config"},
        })
        assert index.surface_liveness()["watcher"] == "declared-in-config"


def test_the_same_numbers_reach_the_html_landing_page():
    """Same URL, same query, two representations (§3.8) — the numbers must
    not exist in JSON only."""
    index = _built_example_index()
    html = render_html.landing_page(index)
    assert "the nine numbers" in html.lower()
    assert "population completeness" in html.lower()
    assert "onboarding cost" in html.lower()
    assert "claim conflicts" in html.lower()


def test_json_and_html_numbers_agree_on_the_transparency_gap_count():
    index = _built_example_index()
    doc = render_json.payload(index, resolve("/"))
    gap = doc["numbers"]["transparency_gap"]
    html = render_html.landing_page(index)
    assert f'{gap["count"]} / {gap["of"]}' in html or f'{gap["count"]}/{gap["of"]}' in html


def test_9_6_names_the_rows_it_counted_in_both_representations():
    """§5.1 evidence field / §3.1 reachability.

    §9.6's members are reachable NOWHERE else on the surface: a staleness
    violation is by definition a row whose state is not in
    `render/html.py::EXCEPTION_STATES`, so it never lands on the
    exception-first view. `count / of` alone is a defect nobody can locate.
    """
    from datetime import datetime, timedelta, timezone

    from console.model.entity import Entity, Provenance
    from console.model.envelope import AdapterResult, AdapterStatus
    from console.model.kinds import Kind, State

    now = datetime.now(timezone.utc)
    index = _built_example_index()
    index.add_result(AdapterResult(
        name="fixture", status=AdapterStatus.OK,
        entities=(Entity(
            kind=Kind.COMPONENT, id="silently-stale-component",
            state=State.HEALTHY,
            provenance=Provenance("checks",
                                  as_of=(now - timedelta(days=8)).isoformat()),
            detail={"cadence_minutes": 60},
        ),),
    ))

    numbers = render_json.payload(index, resolve("/"))["numbers"]
    assert numbers["staleness_honesty"]["count"] == 1
    assert numbers["staleness_honesty"]["violations"] == [
        "silently-stale-component"]

    page = render_html.landing_page(index)
    assert "silently-stale-component" in page


#: Every §9 number that counts a POPULATION OF NAMED ROWS, mapped to the key
#: carrying its count and the key carrying its members. The invariant below is
#: the general form of the two defects fixed one number at a time
#: (alpha-engine-config-I6970/I7107 for §9.1, I7019/nousergon-console-PR86 for
#: §9.6): a count without its members reports a finding nobody can locate, and
#: it is discovered only when someone has to rebuild the index by hand on the
#: box to answer "which row?".
#:
#: Adding a member-naming number here is deliberately cheap. Adding a counting
#: number and NOT listing it here is the thing this table exists to make
#: visible in review.
MEMBER_NAMING = (
    ("population_completeness", "unregistered", "unregistered_ids"),
    ("population_completeness", "rendered_gap", "unrendered_ids"),
    ("staleness_honesty", "count", "violations"),
)


def _count_for(number: dict, count_key: str) -> int:
    """`rendered_gap` is `of - rendered`: §9.1 publishes the PASSING count, so
    the population needing members is the complement."""
    if count_key == "rendered_gap":
        of = number.get("of")
        return 0 if of is None else of - number["rendered"]
    return number[count_key]


def test_a_nonzero_count_always_carries_a_nonempty_member_list():
    """§5.1's evidence field, applied to a number rather than a row.

    Asserted over a REAL index carrying a genuine violation of each kind, not a
    hand-built dict — the defect being guarded lives at the render boundary,
    where the members were computed and then dropped.
    """
    from datetime import datetime, timedelta, timezone

    from console.model.entity import Entity, Provenance
    from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
    from console.model.kinds import Kind, State

    now = datetime.now(timezone.utc)
    index = _built_example_index()
    # One UNREGISTERED component (§9.1) and one silently-stale row (§9.6).
    index.add_result(AdapterResult(
        name="discovery", status=AdapterStatus.OK, claim_class=ClaimClass.DISCOVERY,
        entities=(
            Entity(kind=Kind.COMPONENT, id="wild-component", state=State.UNREPORTED,
                   provenance=Provenance("discovery")),
            Entity(kind=Kind.COMPONENT, id="silently-stale-component",
                   state=State.HEALTHY,
                   provenance=Provenance("checks",
                                         as_of=(now - timedelta(days=8)).isoformat()),
                   detail={"cadence_minutes": 60}),
        ),
    ))
    numbers = render_json.payload(index, resolve("/"))["numbers"]
    assert numbers["population_completeness"]["unregistered"] >= 1
    assert numbers["staleness_honesty"]["count"] >= 1

    for key, count_key, members_key in MEMBER_NAMING:
        number = numbers[key]
        assert members_key in number, f"{key}.{members_key} missing entirely"
        members = number[members_key]
        count = _count_for(number, count_key)
        assert len(members) == count, (
            f"{key}: {count_key}={count} but {members_key} names {len(members)}")
        if count:
            assert all(isinstance(m, str) and m for m in members), key


def test_the_members_appear_in_the_html_representation_too():
    """§3.8 — the same URL, both renderings, the same facts. A member list
    published only on the wire leaves the human reader where the agent was."""
    from console.model.entity import Entity, Provenance
    from console.model.envelope import AdapterResult, AdapterStatus, ClaimClass
    from console.model.kinds import Kind, State

    index = _built_example_index()
    index.add_result(AdapterResult(
        name="discovery", status=AdapterStatus.OK, claim_class=ClaimClass.DISCOVERY,
        entities=(Entity(kind=Kind.COMPONENT, id="wild-component",
                         state=State.UNREPORTED, provenance=Provenance("discovery")),),
    ))
    page = render_html.landing_page(index)
    assert "wild-component" in page
    assert "/component?state=UNREGISTERED" in page
