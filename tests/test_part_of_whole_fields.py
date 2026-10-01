"""alpha-engine-config-I11805: a field declared `of` a sibling renders as a
bar plus "value / whole (pct)" on lists and on the entity page."""
from __future__ import annotations

from console.model.entity import Entity, Provenance
from console.model.fields import parse
from console.model.kinds import Kind, State
from console.records_shape import build_fields
from console.render import html as render_html


def _ent(fields):
    return Entity(kind=Kind.RUN, id="cost-spend:aws", state=State.HEALTHY,
                  provenance=Provenance(source="t"), detail={"fields": fields})


SPEND = {
    "mtd_usd": {"value": 12.5, "unit": "usd", "render": "value", "of": "budget_usd"},
    "budget_usd": {"value": 50, "unit": "usd", "render": "value"},
}


def test_s3_records_carries_of_through():
    out = build_fields({"m": 1, "b": 2},
                       {"m": {"path": "m", "of": "b", "unit": "usd"},
                        "b": {"path": "b", "unit": "usd"}}, None)
    assert out["m"]["of"] == "b" and "of" not in out["b"]
    assert parse(out)[1].of == "b"  # sorted: b, m


def test_list_cell_renders_a_bar_and_the_numbers():
    html = render_html._table([_ent(SPEND)], with_fields=True)
    assert '<progress value="12.5" max="50"></progress>' in html
    assert "12.5 / 50 usd (25%)" in html


def test_over_the_whole_says_so_in_text():
    over = dict(SPEND, mtd_usd=dict(SPEND["mtd_usd"], value=60))
    html = render_html._table([_ent(over)], with_fields=True)
    assert "(120% — over)" in html
    assert '<progress value="50" max="50">' in html


def test_a_zero_whole_is_named_not_divided():
    zero = {"m": {"value": 239.6, "unit": "minutes", "render": "value", "of": "b"},
            "b": {"value": 0, "unit": "usd", "render": "value"}}
    assert "over a zero budget" in render_html._table([_ent(zero)], with_fields=True)


def test_entity_page_renders_the_bar_too():
    assert "<progress" in render_html.fields_section(_ent(SPEND))


def test_a_non_numeric_partner_falls_back_to_plain_text():
    text = {"m": {"value": 3, "unit": "usd", "render": "value", "of": "b"},
            "b": {"value": None, "unit": "usd", "render": "value"}}
    html = render_html._table([_ent(text)], with_fields=True)
    assert "<progress" not in html and "3 usd" in html
