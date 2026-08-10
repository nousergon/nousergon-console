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
    "surface_liveness",          # §9.7 — legitimately N/A-NOT-IMPL this cycle
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


def test_surface_liveness_renders_the_na_not_impl_token_with_a_named_cycle():
    """§9.7 is explicitly out of this issue's scope (it belongs to the deploy
    issue) — the carve-out requires the number be named and the cycle it is
    expected by stated, not silently omitted."""
    index = _built_example_index()
    numbers = render_json.payload(index, resolve("/"))["numbers"]
    assert numbers["surface_liveness"]["state"] == "N/A-NOT-IMPL"
    assert numbers["surface_liveness"]["expected_cycle"]


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
