"""§9.5 orphan count, both directions (console-policy.md §4.4, §9.5).

Panes whose declared question maps to no entity kind, and entity kinds with
no pane. One direction alone measures only the half that is merely untidy.
"""
from __future__ import annotations

import dataclasses

from console.index.graph import Index
from console.index.numbers import orphan_counts
from console.model.kinds import Kind
from console.render.panes import PANES, Pane, orphan_counts as pane_orphan_counts


def test_the_real_pane_registry_has_no_orphans_in_either_direction():
    result = pane_orphan_counts()
    assert result["pane_orphans"]["count"] == 0
    assert result["kind_orphans"]["count"] == 0


def test_every_kind_has_at_least_one_pane():
    covered = {p.kind for p in PANES if p.kind is not None}
    assert covered == {k.route for k in Kind}


def test_a_pane_naming_an_unknown_kind_is_an_orphan():
    broken = PANES + (Pane("bogus-pane", "a question about nothing real", "not-a-kind"),)
    result = pane_orphan_counts(broken)
    assert result["pane_orphans"]["count"] == 1
    assert "bogus-pane" in result["pane_orphans"]["names"]


def test_a_kind_with_no_pane_is_an_orphan():
    trimmed = tuple(p for p in PANES if p.kind != Kind.SIGNAL.route)
    result = pane_orphan_counts(trimmed)
    assert result["kind_orphans"]["count"] == 1
    assert "signal" in result["kind_orphans"]["names"]


def test_cross_cutting_panes_are_never_orphans():
    """landing/search/doctor answer a question that spans every kind — §4.4's
    carve-out, not a defect."""
    result = pane_orphan_counts()
    landing_kind = next(p.kind for p in PANES if p.name == "landing")
    assert landing_kind is None


def test_index_delegates_to_the_pane_registry():
    idx = Index()
    assert idx.orphan_counts() == orphan_counts(idx) == pane_orphan_counts()
