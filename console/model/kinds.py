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
    "pipeline",
)


#: The one list filter that is NOT a facet (§2.2 facets are fields carried on
#: an entity; a state is the row's own §5.1 verdict). It is admitted alongside
#: `FACETS` by the router so every §8.3 state is navigable as a URL —
#: `/component?state=UNREGISTERED` is the view that reaches §9.1's
#: `unregistered_ids`, and the same URL shape reaches any other state a §9
#: number counts (alpha-engine-config-I7107). Without it the only way to see
#: the rows behind a count was the undifferentiated exception table, which is
#: where a single UNREGISTERED component hid among 96 unreported ones.
#:
#: Deliberately not added to `FACETS`: an adapter emitting a facet literally
#: named `state` would then silently shadow the row's real state, and the
#: facet vocabulary is declared in `config.example.yaml` too.
STATE_FILTER: str = "state"


#: The thirteen-state closed vocabulary is NORMATIVE in observability-policy.md
#: §8.3. This console renders it and does not define one: console-policy.md's
#: superordinate note says §8.3's vocabulary "is the only state vocabulary this
#: policy renders". Adding a member here is a PR against observability-policy.md
#: first, and this enum second.
#:
#: The vocabulary is TOTAL and has no fall-through. §8.3 forbids `UNKNOWN`,
#: `OTHER`, `PENDING` and `N/A` BY NAME — they are the escape hatch it exists to
#: remove, and the fall-through is always eventually rendered green. Where the
#: classifier genuinely cannot place a component the answer is `UNREPORTED`,
#: which is loud, and the component is a finding rather than a blank.
#:
#: The informative content is in the pairs, so nothing here may collapse them:
#: DISABLED vs MISSED (a decision vs a defect) · RETIRED vs ABSENT (a stated
#: absence vs an unexplained one) · NEVER_RAN vs MISSED (untested vs
#: untriggered) · UNREPORTED vs HEALTHY (never, under any circumstance) ·
#: RUNNING vs STALLED (heartbeat current vs overdue) · RUNNING vs HEALTHY (has
#: not ended, added `alpha-engine-config-I6358` — see that issue for why a
#: raw-value carve-out for Run entities was tried first and found structurally
#: infeasible before this state was added).
class State(enum.Enum):
    HEALTHY = "HEALTHY"            # ran inside its window, ended ok
    RUNNING = "RUNNING"            # started, not finished, heartbeat within cadence (or none declared)
    DEGRADED = "DEGRADED"          # completed, but a deliverable or quality signal is short
    FAILED = "FAILED"              # ran and ended non-ok
    STALLED = "STALLED"            # started, never finished, heartbeat past cadence
    MISSED = "MISSED"              # the schedule fired or should have; no run started
    NEVER_RAN = "NEVER_RAN"        # registered and in service, no run in its history
    DISABLED = "DISABLED"          # deliberately off — DECLARED, never inferred
    DEPRECATED = "DEPRECATED"      # end-of-life declared, successor named
    RETIRED = "RETIRED"            # removed on purpose; the row persists so the absence is stated
    ABSENT = "ABSENT"              # the registry expects it; the substrate does not have it
    UNREGISTERED = "UNREGISTERED"  # found running on a substrate with no registry row
    UNREPORTED = "UNREPORTED"      # registered, in service, emitting nothing — the transparency gap


#: The three states a registry `lifecycle` field DECLARES (§8.3: "declared in
#: the registry, never inferred"). An adapter reading a registry maps through
#: this and nothing else; an adapter reading telemetry may never produce one.
DECLARED_LIFECYCLE_STATES: dict[str, State] = {
    "disabled": State.DISABLED,
    "deprecated": State.DEPRECATED,
    "retired": State.RETIRED,
}


#: console-policy.md §5.1: a row carries "exactly one value from §8.3's
#: thirteen-state closed vocabulary, FOR ANYTHING THAT RESOLVES TO A COMPONENT
#: STATE; otherwise the value itself." Component and Run resolve to component
#: states. Artifact, Signal, Decision and Incident do not — an issue is not
#: HEALTHY or FAILED, it is open or closed — so those kinds carry the source's
#: own value verbatim, as a string. That is deliberately NOT a second
#: vocabulary: a second enum would be one, and the superordinate note bars it.
COMPONENT_STATE_KINDS: frozenset[Kind] = frozenset({Kind.COMPONENT, Kind.RUN})

#: Raw values (from the "otherwise the value itself" half) that belong on the
#: exception-first landing view alongside the non-HEALTHY component states.
#: Lower-cased at comparison, so an adapter's casing is not load-bearing.
#:
#: `"absent"` is `drivers/object_store.py`'s own token for "declared and not
#: there" (a component's own artifact binding, confirmed missing) and
#: `adapters/declared_registry.py`'s unmerged-claim default for a registered
#: artifact nothing has observed — a load-bearing artifact that never showed
#: up is exactly what §4.3's exception list exists to surface, and it was
#: reaching the landing page's underlying set without ever appearing on it.
EXCEPTION_VALUES: frozenset[str] = frozenset({
    "stale", "no-freshness-stamp", "no-cadence-declared", "unreadable", "absent",
})
