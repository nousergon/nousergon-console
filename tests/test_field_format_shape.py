"""A declared field's `format` (counts / grouped) and `absent`: a structured
value read from a record renders as one line of text a reader scans, and a
null renders as the declared absence — never as a Python repr, never as zero."""
from __future__ import annotations

from console.model.entity import Entity, Provenance
from console.model.kinds import Kind, State
from console.records_shape import build_fields, format_shape
from console.render import html as render_html

COUNTS = {"Alpha": 4, "Beta": 0, "Gamma": 3, "Delta": None, "Other": 1}
ITEMS = [
    {"ref": "org/a#1", "group": "Gamma", "url": "https://example.com/a/pull/1"},
    {"ref": "org/b#2", "group": "Alpha", "url": "https://example.com/b/pull/2"},
    {"ref": "org/c#3", "group": "Alpha", "url": None},
]
SPEC = {
    "by_group": {"path": "by_group", "render": "text", "format": "counts",
                 "absent": "not recorded"},
    "items": {"path": "items", "render": "text", "format": "grouped",
              "group_by": "group", "item": ["url", "ref"],
              "group_order": ["Alpha", "Beta", "Gamma"], "absent": "not recorded"},
}


def test_counts_keep_source_order_omit_zero_and_add_total():
    assert format_shape(COUNTS, {"format": "counts"}) == "Alpha: 4, Gamma: 3, Other: 1 · total 8"


def test_counts_with_nothing_nonzero_is_total_zero():
    assert format_shape({"Alpha": 0}, {"format": "counts"}) == "total 0"


def test_counts_above_six_digits_are_exact():
    # codex 0637e1cc9ce88ab9: ':g' rendered {'A': 1000001} as '1e+06'.
    assert format_shape({"A": 1000001}, {"format": "counts"}) == "A: 1000001 · total 1000001"
    assert format_shape({"A": 2.5, "B": 1234567.0}, {"format": "counts"}) == "A: 2.5, B: 1234567 · total 1234569.5"


def test_grouped_follows_group_order_and_falls_back_to_the_next_item_key():
    out = format_shape(ITEMS, SPEC["items"])
    assert out == ("Alpha: https://example.com/b/pull/2, org/c#3; "
                   "Gamma: https://example.com/a/pull/1")


def test_grouped_empty_list_is_none():
    assert format_shape([], SPEC["items"]) == "none"


def test_wrong_shape_or_unknown_format_is_returned_unchanged():
    assert format_shape([1, 2], {"format": "counts"}) == [1, 2]
    assert format_shape({"a": 1}, {"format": "nope"}) == {"a": 1}


def test_null_renders_the_declared_absence_not_zero():
    out = build_fields({"by_group": None, "items": None}, SPEC, None)
    assert out["by_group"]["value"] == "not recorded"
    assert out["items"]["value"] == "not recorded"


def test_without_absent_a_null_stays_null():
    out = build_fields({"x": None}, {"x": {"render": "text", "format": "counts"}}, None)
    assert out["x"]["value"] is None


def test_page_links_the_grouped_urls():
    fields = build_fields({"by_group": COUNTS, "items": ITEMS}, SPEC, None)
    ent = Entity(kind=Kind.RUN, id="job@2026-11-02", state=State.HEALTHY,
                 provenance=Provenance(source="t"), detail={"fields": fields})
    html = render_html._table([ent], with_fields=True)
    assert "Alpha: 4, Gamma: 3, Other: 1 · total 8" in html
    assert '<a href="https://example.com/b/pull/2">' in html


def test_length_counts_a_list_or_mapping_and_an_empty_one_is_zero():
    # A reconciliation carries its breaks only as `discrepancies[]`; the home
    # tile shows how many (alpha-engine-config-I12783).
    spec = {"path": "discrepancies", "render": "count", "unit": "breaks", "format": "length"}
    assert format_shape([{"instrument": "SPY"}, {"instrument": "QQQ"}], spec) == 2
    assert format_shape({"a": 1}, spec) == 1
    assert format_shape([], spec) == 0
    out = build_fields({"discrepancies": []}, {"open_breaks": spec}, None)
    assert out["open_breaks"]["value"] == 0 and out["open_breaks"]["unit"] == "breaks"


def test_length_of_a_scalar_or_a_missing_list_is_not_invented():
    spec = {"render": "count", "format": "length"}
    assert format_shape("3", spec) == "3"
    assert build_fields({}, {"open_breaks": spec}, None)["open_breaks"]["value"] is None


def test_cents_reads_exact_cents_as_money():
    assert format_shape(123456, {"format": "cents"}) == 1234.56
    assert format_shape(-10, {"format": "cents"}) == -0.1
    assert format_shape(True, {"format": "cents"}) is True  # never a bool
    assert format_shape("12", {"format": "cents"}) == "12"  # wrong shape: unchanged


def test_pairs_write_each_posting_on_one_line():
    postings = [{"account": "assets:cash:USD", "amount_cents": -1234},
                {"account": "expenses:commissions", "amount_cents": 1234}]
    spec = {"format": "pairs", "key": "account", "value": "amount_cents", "value_format": "cents"}
    assert format_shape(postings, spec) == "assets:cash:USD: -12.34 · expenses:commissions: 12.34"
    assert format_shape([], spec) == "none"
