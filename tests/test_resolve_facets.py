"""Unit coverage for `records_shape.resolve_facets` — literal + path grammar.

The adapter/driver integration coverage lives in `test_s3_records.py`. This
file pins the shared function's own contract, including the refuse-on-typo
cases a path-only loop cannot express.
"""
from __future__ import annotations

import pytest

from console.records_shape import resolve_facets


def test_string_path_reads_the_record():
    assert resolve_facets({"sector": "sector"}, {"sector": "tech"}) == {"sector": "tech"}


def test_path_mapping_reads_the_record():
    assert resolve_facets({"sector": {"path": "sector"}}, {"sector": "tech"}) == {
        "sector": "tech"
    }


def test_missing_path_omits_the_facet():
    assert resolve_facets({"sector": "sector"}, {"id": "x"}) == {}


def test_literal_value_stamps_regardless_of_row():
    assert resolve_facets({"pipeline": {"value": "crucible-board"}}, {"surface": "x"}) == {
        "pipeline": "crucible-board"
    }


def test_str_of_dict_is_the_pre_fix_omit_bug():
    """The FAIL shape: callers used to do get_path(..., str(dict_spec))."""
    assert "pipeline" not in resolve_facets(
        {"pipeline": str({"value": "crucible-board"})},
        {"surface": "crucible/board"},
    )


def test_value_and_path_together_are_refused():
    with pytest.raises(ValueError, match="both `value` and `path`"):
        resolve_facets({"pipeline": {"value": "a", "path": "b"}}, {})


def test_null_literal_is_refused():
    with pytest.raises(ValueError, match="value: null"):
        resolve_facets({"pipeline": {"value": None}}, {})


def test_empty_mapping_is_refused():
    with pytest.raises(ValueError, match="neither `path` nor `value`"):
        resolve_facets({"pipeline": {}}, {})


def test_null_path_is_refused_not_looked_up_as_None():
    """Adversarial FAIL: `{path: null}` used to do get_path(..., str(None))
    → key `"None"`, and with a coincidental field of that name would
    wrong-stamp. Same typo class as `{value: null}`."""
    with pytest.raises(ValueError, match="path: null"):
        resolve_facets({"pipeline": {"path": None}}, {"None": "wrong-stamp"})


def test_non_string_path_is_refused():
    with pytest.raises(ValueError, match="non-string `path`"):
        resolve_facets({"pipeline": {"path": 123}}, {"123": "nope"})



# --- `map`: a path facet translated through a declared map -----------------


def test_mapped_path_translates_a_mapped_value():
    spec = {"pane": {"path": "unit_id", "map": {"standing": "data-collector"}}}
    assert resolve_facets(spec, {"unit_id": "standing"}) == {"pane": "data-collector"}


def test_mapped_path_omits_an_unmapped_value_rather_than_passing_it_through():
    """The FAIL shape the map exists to prevent: a pass-through would stamp
    `pane=D01` on every clause row and split one board into dozens of panes."""
    spec = {"pane": {"path": "unit_id", "map": {"standing": "data-collector"}}}
    assert resolve_facets(spec, {"unit_id": "D01"}) == {}


def test_mapped_path_omits_when_the_path_is_absent():
    spec = {"pane": {"path": "unit_id", "map": {"standing": "data-collector"}}}
    assert resolve_facets(spec, {"clause": "x"}) == {}


def test_mapped_path_matches_on_the_string_form_of_the_value():
    spec = {"tier": {"path": "phase", "map": {"1": "early"}}}
    assert resolve_facets(spec, {"phase": 1}) == {"tier": "early"}


def test_map_beside_a_literal_is_refused():
    with pytest.raises(ValueError, match="beside a literal `value`"):
        resolve_facets({"pane": {"value": "a", "map": {"x": "y"}}}, {})


@pytest.mark.parametrize("bad", [{}, ["standing"], "standing", None])
def test_map_that_is_not_a_non_empty_mapping_is_refused(bad):
    with pytest.raises(ValueError, match="not a non-empty mapping"):
        resolve_facets({"pane": {"path": "unit_id", "map": bad}}, {"unit_id": "x"})


@pytest.mark.parametrize("target", [None, ""])
def test_map_entry_with_an_empty_target_is_refused(target):
    with pytest.raises(ValueError, match="maps 'standing' to an empty value"):
        resolve_facets(
            {"pane": {"path": "unit_id", "map": {"standing": target}}},
            {"unit_id": "standing"},
        )
