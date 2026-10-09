"""alpha-engine-config-I11792: a `render: timeseries` field renders as an
inline SVG bar chart, and an `of:` sibling as a dashed reference line.

Brian 2026-10-09: "graphing the costs of these services daily and putting the
charts in the costs console". The chart is generic (§5.8): it reads the
value's shape, never who emitted it.
"""
from __future__ import annotations

import pytest

from console.model.entity import Entity, Provenance
from console.model.fields import parse, series_points
from console.model.kinds import Kind, State
from console.render import html as render_html


def _ent(fields):
    return Entity(kind=Kind.RUN, id="cost2-aws-daily:s3", state=State.DEGRADED,
                  provenance=Provenance(source="t"), detail={"fields": fields})


DAILY = {
    "daily_usd": {
        "value": [{"date": "2026-10-01", "usd": 0.74}, {"date": "2026-10-02", "usd": 1.01},
                  {"date": "2026-10-03", "usd": 2.68}],
        "unit": "usd", "render": "timeseries", "baseline": None, "of": "daily_budget_usd",
    },
    "daily_budget_usd": {"value": 0.9677, "unit": "usd", "render": "value", "baseline": None},
}


@pytest.mark.parametrize(
    "value, expected",
    [
        ([1, 2.5], [("1", 1.0), ("2", 2.5)]),
        ({"2026-10-01": 0.5, "2026-10-02": None}, [("2026-10-01", 0.5), ("2026-10-02", None)]),
        ([["a", 1], ["b", 2]], [("a", 1.0), ("b", 2.0)]),
        ([{"date": "d1", "usd": 1}], [("d1", 1.0)]),
        # `value` wins over another numeric key.
        ([{"x": "a", "value": 3, "n": 9}], [("a", 3.0)]),
        # The pipeline-reliability shape: a null y with a reason string is a gap.
        ([{"date": "d1", "buffer_minutes": 12.5, "reason": None},
          {"date": "d2", "buffer_minutes": None, "reason": "no-completion"}],
         [("d1", 12.5), ("d2", None)]),
    ],
)
def test_series_points_reads_the_shape(value, expected):
    assert series_points(value) == expected


@pytest.mark.parametrize("value", [
    [], None, "abc", [{"date": "d", "a": 1, "b": 2}],  # two y candidates: ambiguous
    [{"no_x": 1}], [{"date": "d"}],
])
def test_a_shape_with_no_single_reading_is_not_charted(value):
    assert series_points(value) is None


def test_list_cell_renders_bars_and_a_reference_line():
    html = render_html._table([_ent(DAILY)], with_fields=True)
    assert '<svg class="ts-chart"' in html
    assert html.count("<rect ") == 3
    assert '<line class="ts-ref"' in html
    assert "<title>2026-10-03: 2.68 usd</title>" in html


def test_the_numbers_are_in_text_beside_the_chart():
    html = render_html._table([_ent(DAILY)], with_fields=True)
    assert ("3 points, 2026-10-01 – 2026-10-03 · latest 2.68 usd · peak 2.68 usd · "
            "daily_budget_usd 0.9677 usd (2 of 3 above)") in html


def test_entity_page_renders_the_chart_too():
    assert '<svg class="ts-chart"' in render_html.fields_section(_ent(DAILY))


def test_no_reference_without_an_of_partner():
    plain = {"daily_usd": dict(DAILY["daily_usd"], of=None)}
    html = render_html._table([_ent(plain)], with_fields=True)
    assert '<svg class="ts-chart"' in html and "ts-ref" not in html


def test_a_null_point_is_a_gap_not_a_zero_bar():
    gappy = {"s": {"value": [1, None, 2], "unit": "minutes", "render": "timeseries"}}
    html = render_html._table([_ent(gappy)], with_fields=True)
    assert html.count("<rect ") == 2


def test_negative_values_draw_below_the_zero_line():
    neg = {"s": {"value": [-5, 5], "unit": "minutes", "render": "timeseries"}}
    html = render_html._table([_ent(neg)], with_fields=True)
    assert html.count("<rect ") == 2 and "peak 5 minutes" in html


def test_an_unchartable_value_falls_back_to_the_point_count():
    odd = {"s": {"value": [{"date": "d", "a": 1, "b": 2}], "unit": "u", "render": "timeseries"}}
    html = render_html._table([_ent(odd)], with_fields=True)
    assert "<svg" not in html and "1 points u" in html


def test_labels_are_escaped():
    evil = {"s": {"value": {"<script>": 1}, "unit": "u", "render": "timeseries"}}
    html = render_html._table([_ent(evil)], with_fields=True)
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_the_chart_is_not_coloured_as_a_verdict():
    """§5.4: `of` is a relation, not a baseline — no fill colour in the markup."""
    html = render_html._table([_ent(DAILY)], with_fields=True)
    svg = html[html.index("<svg"):html.index("</svg>")]
    assert "fill=" not in svg and "stroke=" not in svg
    (field, _) = parse(DAILY)[::-1]  # sorted: daily_budget_usd, daily_usd
    assert not field.comparable
