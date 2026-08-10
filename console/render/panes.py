"""The declared pane registry (§4.4, §4.5) and the §9.5 orphan check.

Every pane declares the question it answers and, where the question is about
one entity kind rather than the whole index, which kind — the §4.4 test made
data rather than left as a review convention. `None` means the pane is
cross-cutting (landing, search, doctor, a registry page): §4.4's own carve-out
for a question that spans every kind, and cross-cutting is not the same as
orphaned.

The per-kind list and entity panes are declared ONCE, generated over `Kind`
(§3.5) rather than hand-listed one at a time — adding an eighth kind is a PR
against `console-policy.md` §2.1 first, and this registry follows it with no
edit of its own, which is the same "no console change" property §2.6 asks of
onboarding a component, applied to onboarding a KIND.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..model.kinds import Kind


@dataclass(frozen=True)
class Pane:
    """One declared pane. `kind` is a `Kind.route` string (not a `Kind`
    instance) on purpose — a real descriptor would name it as configuration,
    and a string is what lets `test_a_pane_naming_an_unknown_kind_is_an_orphan`
    exercise a genuinely invalid reference rather than one Python's type
    system already forbids."""

    name: str
    question: str
    kind: str | None  # a Kind.route string, or None for cross-cutting


#: Cross-cutting panes: their question is about the whole surface, not one
#: kind, which §4.4's carve-out admits explicitly.
_CROSS_CUTTING: tuple[Pane, ...] = (
    Pane("landing", "is there anything I need to look at right now", None),
    Pane("search", "where is X", None),
    Pane("doctor", "why is X not on the surface", None),
    Pane("registry", "what does this registry declare", None),
)

PANES: tuple[Pane, ...] = _CROSS_CUTTING + tuple(
    pane
    for k in Kind
    for pane in (
        Pane(f"{k.route}-list", f"what {k.value}s exist and what state are they in", k.route),
        Pane(f"{k.route}-entity", f"everything known about one {k.value}", k.route),
    )
)


def orphan_counts(panes: tuple[Pane, ...] = PANES) -> dict[str, object]:
    """§9.5 — both directions. Target 0 / 0.

    ``panes`` defaults to the real registry above; a test passes a mutated
    copy to prove each direction actually catches something (§2.5's own
    testing pattern — break one thing, watch one count move).
    """
    valid_routes = {k.route for k in Kind}
    pane_orphans = sorted(
        p.name for p in panes if p.kind is not None and p.kind not in valid_routes
    )
    covered = {p.kind for p in panes if p.kind is not None}
    kind_orphans = sorted(k.value for k in Kind if k.route not in covered)
    return {
        "pane_orphans": {"count": len(pane_orphans), "of": len(panes), "names": pane_orphans},
        "kind_orphans": {"count": len(kind_orphans), "of": len(list(Kind)), "names": kind_orphans},
    }
