"""Search tests — exact-first ranking, all kinds in scope, true negatives."""
from __future__ import annotations

from console.index.graph import Index
from console.model.envelope import AdapterResult, AdapterStatus
from console.search.resolve import search
from tests.fixtures import artifact, component


def _index() -> Index:
    ents = [
        component("comp-alpha"),
        component("comp-alpine"),
        artifact("s3://b/comp-alpha/output.json"),
    ]
    idx = Index()
    idx.add_result(AdapterResult(name="f", status=AdapterStatus.OK,
                                 entities=tuple(ents)))
    return idx


def test_exact_match_ranks_first():
    hits = search(_index(), "comp-alpha")
    assert hits[0].entity.id == "comp-alpha"
    assert hits[0].exact is True


def test_prefix_and_substring_candidates():
    hits = search(_index(), "comp-al")
    ids = {h.entity.id for h in hits}
    assert "comp-alpha" in ids and "comp-alpine" in ids


def test_identifier_with_slashes_resolves():
    hits = search(_index(), "comp-alpha/output.json")
    assert any(h.entity.id == "s3://b/comp-alpha/output.json" for h in hits)


def test_empty_query_returns_nothing():
    assert search(_index(), "   ") == []


def test_true_negative_is_empty_not_excluded_kind():
    # A query for something not in the fleet returns nothing — a true
    # negative, distinguishable from a kind being silently out of scope.
    assert search(_index(), "nonexistent-thing") == []
