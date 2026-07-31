"""The seven closed entity kinds and the facet vocabulary.

`Kind` is closed by policy (§2.1): adding an eighth is a PR against
`console-policy.md`, not a value added here. The closed set is the mechanism
that keeps placement constrained — an informally widened model no longer
constrains where a fact goes, and placement is the whole point.

Facets (§2.2) are fields carried on an entity and sliced uniformly by the
index. They are declared here so the index, the router and the facet controls
share one vocabulary and no pane hand-rolls a filter.
"""
from __future__ import annotations

import enum


class Kind(enum.Enum):
    """The seven entity kinds, in the policy's declared order (§2.1)."""

    COMPONENT = "component"
    RUN = "run"
    CYCLE = "cycle"
    ARTIFACT = "artifact"
    SIGNAL = "signal"
    DECISION = "decision"
    INCIDENT = "incident"

    @property
    def route(self) -> str:
        """The URL segment for this kind — `/<kind>/<id>` (§3.2).

        Routes are the literal kind names, never numeric or positional ids,
        which §3.2 forbids because they break the moment a pane is added.
        """
        return self.value

    @classmethod
    def from_route(cls, segment: str) -> "Kind | None":
        """Resolve a URL segment to a kind, or None if it names none."""
        for kind in cls:
            if kind.route == segment:
                return kind
        return None


#: Facets (§2.2) — the slicing dimensions every list can be filtered by,
#: uniformly, with no pane implementing its own filtering. The example config
#: (`config.example.yaml`) declares the same set; keep them in step.
FACETS: tuple[str, ...] = (
    "owner",
    "substrate",
    "repo",
    "environment",
    "authority_tier",
    "lifecycle",
)


#: The twelve-state closed vocabulary is normative in observability-policy.md
#: §8.3; the console renders it (console-policy.md §5.1) and does not redefine
#: it. The subset the surface needs to distinguish for staleness and the
#: exception list is named here so renderers share one spelling. "No data" is
#: its own state and is never drawn as green and never as nothing (§5.5).
class State(enum.Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILING = "FAILING"
    STALE = "STALE"
    UNREPORTED = "UNREPORTED"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"
    NOT_MEASURED = "NOT_MEASURED"
    NEVER_RAN = "NEVER_RAN"
    FAILED = "FAILED"  # an adapter whose source is unreachable (§2.3)
    NA_NOT_IMPL = "N/A-NOT-IMPL"  # §11 carve-out, first cycle only
    UNREGISTERED = "UNREGISTERED"  # discovered but not in the registry
