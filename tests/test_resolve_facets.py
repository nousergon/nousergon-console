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
