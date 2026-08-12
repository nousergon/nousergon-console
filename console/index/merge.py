"""Claim resolution — several sources describing one entity (§2.5).

**Two adapters describing the same thing is the normal case, not a collision.**
A registry *declares* a component, telemetry *observes* it, a substrate
enumeration *discovers* it: three sources, one `component_id`. The index that
treats the second arrival as an error has converted the most common onboarding
event into an outage, and it cannot compute `UNREGISTERED` or `ABSENT` at all —
both of those require a declaration claim and a discovery claim about one
identifier to coexist.

So an adapter returns **claims**, and this module resolves them:

- **Precedence is by claim class** — declaration > observation > discovery
  (`envelope.ClaimClass`). Lower rank wins a contested field.
- **Provenance survives, per field.** A merged row names three sources, one per
  field, rather than collapsing to whichever adapter ran last (§5.1).
- **A loser is retained, not discarded.** `Entity.superseded` keeps what each
  losing claim proposed, so the disagreement is visible on the entity page.
- **An equal-rank disagreement is a conflict**, rendered as `DEGRADED` with the
  field named — never resolved by arrival order, and never fatal (§9.9).
- **A lifecycle disposition may only come from a declaration.**
  `observability-policy.md` §8.3: `DISABLED`, `DEPRECATED` and `RETIRED` are
  declared in the registry, **never inferred**. A non-declaration claim
  proposing one is superseded and the entity falls back to `UNREPORTED`, which
  is loud. The proposal stays visible — "systemd says this unit is masked" is
  worth reading even when nothing authoritative has said it should be off.
- **A genuine collision still fails.** Two claims with different `kind` for one
  identifier is `§3.6` violated, and it raises. `kind` disagreement is the
  discriminator between "one thing seen twice" and "two things, one name".
"""
from __future__ import annotations

import dataclasses

from ..model.entity import Entity, Provenance
from ..model.envelope import ClaimClass
from ..model.kinds import DECLARED_LIFECYCLE_STATES, State

#: The three states that may only ever arrive on a DECLARATION claim (§8.3).
DECLARED_ONLY: frozenset[State] = frozenset(DECLARED_LIFECYCLE_STATES.values())

#: The fields merged independently. `state` and `provenance` are handled
#: specially; these are the plain ones where a value either exists or does not.
_MERGED_FIELDS = ("facets", "detail")


class NamespaceCollision(Exception):
    """Two DIFFERENT things share an identifier (§3.6).

    Not raised when two sources describe the SAME thing — that is a merge, and
    it is the case this module exists for. Raised only on a `kind` disagreement,
    which is the one form a duplicate identifier takes that no precedence rule
    can honestly resolve.
    """


@dataclasses.dataclass(frozen=True)
class Claim:
    """One adapter's statement about one entity.

    ``reachable`` is False when the adapter's source could not be read this
    pass. Such a claim is retained — the row must not vanish (§5.5) — but it is
    **not a peer** in any contest: a source declaring its own blindness is not
    disagreeing about the fleet, and treating it as a disagreement would turn
    every transient outage into a fleet-wide `DEGRADED`.
    """

    entity: Entity
    claim_class: ClaimClass
    adapter: str
    reachable: bool = True


def merge(claims: list[Claim]) -> Entity:
    """Resolve every claim about one identifier into one entity.

    Claims arrive in ingest order and the result does not depend on it — the
    merge sorts by rank first, which is what makes the surface reproducible
    across adapter reordering.
    """
    if not claims:  # pragma: no cover - callers hold a non-empty list
        raise ValueError("merge() called with no claims")

    kinds = {c.entity.kind for c in claims}
    if len(kinds) > 1:
        names = ", ".join(sorted(k.value for k in kinds))
        raise NamespaceCollision(
            f"identifier {claims[0].entity.id!r} is claimed as {names} — two "
            "different things share one name (§3.6). This is not a merge: no "
            "precedence rule can resolve a kind disagreement honestly."
        )

    if len(claims) == 1:
        return claims[0].entity

    ordered = sorted(claims, key=lambda c: c.claim_class.rank)
    base = ordered[0].entity

    field_sources: dict[str, Provenance] = {}
    superseded: list[tuple[str, object, Provenance]] = []
    conflicts: list[str] = []

    state, state_extras = _merge_state(ordered)
    superseded.extend(state_extras.superseded)
    conflicts.extend(state_extras.conflicts)
    field_sources["state"] = state_extras.winner

    # Deliberately NOT seeded from `base`: a value copied in before the loop is
    # a value whose winning claim is never recorded, and §5.1's per-field source
    # then silently falls back to the entity's own provenance. Filling from
    # empty in rank order is what makes `source_of` truthful.
    facets: dict[str, str] = {}
    detail: dict[str, object] = {}
    for name, sink in (("facets", facets), ("detail", detail)):
        for claim in ordered:
            for key, value in getattr(claim.entity, name).items():
                if key not in sink:
                    sink[key] = value
                    field_sources[f"{name}.{key}"] = claim.entity.provenance
                elif sink[key] != value:
                    winner = field_sources.get(f"{name}.{key}", base.provenance)
                    if winner is claim.entity.provenance:
                        continue
                    superseded.append(
                        (f"{name}.{key}", value, claim.entity.provenance)
                    )
                    if _same_rank_as_winner(ordered, claim, field_sources,
                                            f"{name}.{key}"):
                        conflicts.append(f"{name}.{key}")

    # The freshest as-of across claims is the honest one: it is the most recent
    # moment any source could speak to this entity. Its evidence link travels
    # with it, so the row's "how do you know" and "go look" stay consistent.
    freshest = _freshest(ordered)
    field_sources.setdefault("as_of", freshest)

    if conflicts and isinstance(state, State) and state not in DECLARED_ONLY:
        # §2.5: an unresolved equal-rank disagreement renders DEGRADED with the
        # field named. It is not FAILED — nothing is broken; two sources
        # disagree, which is itself a fact about the fleet worth showing.
        #
        # EXCEPT over a declared lifecycle. §8.3 makes `DISABLED`/`DEPRECATED`/
        # `RETIRED` declared and never inferred, and `_state_rank` already gives
        # a declaration carrying one absolute precedence — but this line ran
        # afterwards and overrode it whenever any OTHER field disagreed.
        # Measured 2026-08-12 (alpha-engine-config-I7061): four components whose
        # EventBridge rule and target Lambda share one name rendered DEGRADED on
        # a `detail` disagreement between two observations, while their registry
        # rows declared them deliberately off under a named ruling. A decision
        # was overridden by a field neither source disagreed about the meaning
        # of. The conflict is still recorded and still rendered on the entity —
        # it just no longer replaces the disposition.
        state = State.DEGRADED

    return dataclasses.replace(
        base,
        state=state,
        provenance=freshest,
        facets=facets,
        detail=detail,
        field_sources=field_sources,
        superseded=tuple(superseded),
        conflicts=tuple(sorted(set(conflicts))),
    )


@dataclasses.dataclass(frozen=True)
class _StateResolution:
    winner: Provenance
    superseded: tuple[tuple[str, object, Provenance], ...]
    conflicts: tuple[str, ...]


def _state_rank(claim: Claim) -> int:
    """Precedence for the `state` field specifically — NOT the claim rank.

    §2.5's table says a declaration supplies **existence, lifecycle, owner,
    authority tier, cadence and retention**. It does not supply *state*, and
    treating it as though it did inverts the vocabulary: a registry row's
    `UNREPORTED` ("registered, in service, emitting nothing") would beat live
    telemetry's `HEALTHY`, and every observed component would render as a
    transparency gap. The whole surface would report itself blind.

    So for state, and only for state:

    0. a **declaration carrying a declared lifecycle** — `DISABLED`,
       `DEPRECATED`, `RETIRED`. Absolute: §8.3 says these are declared and
       never inferred, and a decision must not be overridden by telemetry that
       structurally cannot tell a decision from a defect.
    1. an **observation** — what actually ran. This is the normal winner.
    2. a **discovery** — the substrate says it is there and little else.
    3. a **declaration without a lifecycle** — it asserted existence, not
       health, so it is the LAST word on state rather than the first. Its
       `UNREPORTED` is then the correct answer only when nothing observed it,
       which is precisely what that state means.
    4. an **unreachable source** — it is speaking about itself, not about the
       entity, so it never displaces a live reading and never contests one.
    """
    st = claim.entity.state
    if not claim.reachable:
        # An unreachable source has no opinion about the entity, only about
        # itself. It ranks below everything so a live reading always wins.
        return 4
    if claim.claim_class is ClaimClass.DECLARATION:
        if isinstance(st, State) and st in DECLARED_ONLY:
            return 0
        return 3
    if claim.claim_class is ClaimClass.OBSERVATION:
        return 1
    return 2


def _merge_state(ordered: list[Claim]) -> tuple[State | str, _StateResolution]:
    """Resolve `state` under §2.5 precedence, with the §8.3 lifecycle guard.

    The guard is the load-bearing part and it is why this is not a one-liner: a
    `DISABLED`, `DEPRECATED` or `RETIRED` proposed by anything but a declaration
    is **superseded**, because §8.3 says those three are declared and never
    inferred. A defect that renders as a decision stops paging anybody, and it
    is the one collapse §8.3 calls out by name.

    The proposal is retained rather than dropped — "systemd says this unit is
    masked" is worth reading even when nothing authoritative has said it should
    be off, and a reader who can see both learns something a single verdict
    cannot tell them.
    """
    superseded: list[tuple[str, object, Provenance]] = []
    conflicts: list[str] = []

    admissible: list[Claim] = []
    for claim in ordered:
        st = claim.entity.state
        if (
            isinstance(st, State)
            and st in DECLARED_ONLY
            and claim.claim_class is not ClaimClass.DECLARATION
        ):
            superseded.append(("state", st, claim.entity.provenance))
            continue
        admissible.append(claim)

    if not admissible:
        # Every claim proposed a declared-only state and none was a declaration.
        # Nothing authoritative has said this is deliberately off, so it is not
        # DISABLED — it is unplaceable, which §8.3 renders UNREPORTED.
        return State.UNREPORTED, _StateResolution(
            ordered[0].entity.provenance, tuple(superseded), ()
        )

    ranked = sorted(admissible, key=_state_rank)
    best = ranked[0]
    for claim in ranked[1:]:
        if claim.entity.state == best.entity.state:
            continue
        superseded.append(("state", claim.entity.state, claim.entity.provenance))
        if (
            claim.reachable
            and best.reachable
            and _state_rank(claim) == _state_rank(best)
        ):
            conflicts.append("state")

    return best.entity.state, _StateResolution(
        best.entity.provenance, tuple(superseded), tuple(conflicts)
    )


def _same_rank_as_winner(
    ordered: list[Claim],
    claim: Claim,
    field_sources: dict[str, Provenance],
    key: str,
) -> bool:
    winner_prov = field_sources.get(key)
    for other in ordered:
        if other.entity.provenance is winner_prov:
            return other.claim_class.rank == claim.claim_class.rank
    return False


def _freshest(ordered: list[Claim]) -> Provenance:
    """The provenance carrying the most recent as-of, else the best-ranked.

    A merged row's as-of is when the underlying fact was last true according to
    *some* source (§5.1), so taking the highest-ranked claim's stamp would
    report a registry file's absent freshness over live telemetry's.
    """
    stamped = [c for c in ordered if c.entity.provenance.as_of]
    if not stamped:
        return ordered[0].entity.provenance
    return max(stamped, key=lambda c: c.entity.provenance.as_of or "").entity.provenance
