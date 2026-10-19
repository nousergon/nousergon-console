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


# ---- declared labels (alpha-engine-config-I11805) ---------------------------

def _labelled_index() -> Index:
    from dataclasses import replace

    ents = [
        component("comp-alpha"),
        replace(component("ae-export.service"), detail={"label": "Nightly export"}),
        replace(component("ae-ingest.service"), detail={"label": "Export ingest backfill"}),
        # Its id matches the query below, so the label must not move it.
        replace(component("nightly-sweep"), detail={"label": "Sweep"}),
    ]
    idx = Index()
    idx.add_result(AdapterResult(name="f", status=AdapterStatus.OK,
                                 entities=tuple(ents)))
    return idx


def test_a_declared_label_is_searchable_by_any_case():
    hits = search(_labelled_index(), "NIGHTLY EXPORT")
    assert [h.entity.id for h in hits] == ["ae-export.service"]
    assert hits[0].exact is False and hits[0].matched_on == "label"


def test_label_words_match_in_any_order():
    ids = [h.entity.id for h in search(_labelled_index(), "export nightly")]
    assert ids == ["ae-export.service"]


def test_identifier_matches_rank_before_every_label_match():
    # §3.7: an identifier still lands first; labels only add hits after it.
    hits = search(_labelled_index(), "nightly")
    assert [h.entity.id for h in hits] == ["nightly-sweep", "ae-export.service"]
    assert [h.matched_on for h in hits] == ["id", "label"]


def test_label_rank_is_equal_then_prefix_then_words():
    hits = search(_labelled_index(), "export")
    assert [h.entity.id for h in hits] == ["ae-export.service", "ae-ingest.service"]
    # ae-export.service matched on its id; the other only on its label.
    assert [h.matched_on for h in hits] == ["id", "label"]
    hits = search(_labelled_index(), "export ingest backfill")
    assert [h.entity.id for h in hits] == ["ae-ingest.service"]


def test_a_label_never_makes_an_exact_hit():
    # Exact is reserved for the identifier (§3.7), so reachability and
    # `doctor`, which read `exact`, are unchanged by labels.
    assert not any(h.exact for h in search(_labelled_index(), "Sweep"))
