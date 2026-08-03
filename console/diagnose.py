"""`doctor` — the surface explains its own absences (§3.9).

**When something is not on the console, the console says why.**

Onboarding fails *silently* by construction. Nothing raises when a module is
absent; the absence has exactly the shape of `observability-policy.md` §8.3's
`UNREPORTED` — a fact nobody can distinguish from "there is nothing to show" —
and the person debugging it has no thread to pull but adapter source. That is
`principles.md` §2.3 unmet at the onboarding boundary: **detected, never
diagnosed.** It is also the difference between a surface anyone can extend and
one only its author can, which is what §2.6's whole "onboarding costs nothing"
claim rests on the moment somebody mistypes a config key.

So `doctor` walks the chain that would have put an identifier on the surface
and names the **first** broken link:

    registry row → adapter claim → merged entity
                 → search-reachable → structure-reachable → relation-reachable

Two properties make the answer worth having:

- **It names what to do next**, not a boolean. "No adapter returned a claim"
  is a fact; "none of `registry`, `checks` returned a claim for this id — the
  emission may not be landing where `checks` reads" is a next step.
- **It re-derives the chain per query.** A persists-nothing design is *better*
  placed for this than a database-backed one (`console-policy.md` §10.5): there
  is no ingest state to be stale, so a diagnosis is never a report about a
  snapshot somebody took an hour ago.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .index.graph import Index
from .model.envelope import ClaimClass
from .search.resolve import search


@dataclass(frozen=True)
class Step:
    """One link in the chain, and what to do if it is the broken one."""

    name: str
    ok: bool
    detail: str
    remedy: str | None = None


@dataclass(frozen=True)
class Diagnosis:
    identifier: str
    steps: tuple[Step, ...] = ()
    #: The first broken link, or None when the identifier is fully reachable.
    #: "First" is the load-bearing word: a chain reporting every failure at
    #: once buries the one that caused the others.
    broken: Step | None = None
    facts: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.broken is None

    def summary(self) -> str:
        if self.ok:
            return f"{self.identifier}: on the surface, reachable on all three paths"
        return f"{self.identifier}: {self.broken.name} — {self.broken.detail}"


def doctor(index: Index, identifier: str) -> Diagnosis:
    """Diagnose why ``identifier`` is or is not on the surface.

    Never raises, including for an identifier the fleet has never heard of —
    "nothing knows this name" is a legitimate and useful answer, and it is the
    most common one when somebody is debugging a typo.
    """
    index.finalize()
    claims = index.claims_for(identifier)
    adapters_asked = [a.name for a in index.build_info.adapters]
    steps: list[Step] = []

    # 1. Declared?
    declared = [c for c in claims if c.claim_class is ClaimClass.DECLARATION]
    steps.append(Step(
        name="registry row",
        ok=bool(declared),
        detail=(
            f"declared by {', '.join(c.adapter for c in declared)}"
            if declared else "no registry adapter declared this identifier"
        ),
        remedy=None if declared else (
            "add a row for it in the registry directory the console reads "
            "(`registry:` in config.yaml). Without one it can still render, "
            "but as UNREGISTERED — it is outside the denominator, so no "
            "completeness number counts it (§2.4)."
        ),
    ))

    # 2. Observed or discovered by anything?
    #
    # Deliberately NOT "any claim at all": a declaration says the thing should
    # exist, and telemetry says whether it does. A component declared and never
    # observed is `UNREPORTED` — it renders, and the useful diagnosis is that
    # nothing has reported on it, which a check for "any claim" would call a
    # pass.
    reporting = [c for c in claims if c.claim_class is not ClaimClass.DECLARATION]
    steps.append(Step(
        name="adapter claim",
        ok=bool(reporting),
        detail=(
            f"{len(reporting)} claim(s) from "
            f"{', '.join(sorted({c.adapter for c in reporting}))}"
            if reporting else
            f"nothing reported on it; {len(adapters_asked)} source(s) were "
            f"asked ({', '.join(adapters_asked) or 'none enabled'})"
        ),
        remedy=None if reporting else (
            "check that the emission is landing where an enabled adapter "
            "reads, and that the adapter's key_pattern matches the path it is "
            "written to. An adapter that ran fine and found nothing looks "
            "exactly like one that was never enabled — compare its status in "
            "the index freshness block."
        ),
    ))

    # 3. Did it survive the merge?
    entity = index.entity(identifier)
    steps.append(Step(
        name="merged entity",
        ok=entity is not None,
        detail=(
            f"{entity.kind.value} in state {entity.state_value}"
            if entity else "claims exist but no entity was produced"
        ),
        remedy=None if entity is not None else (
            "claims for this identifier did not resolve to one entity (§2.5). "
            "The usual cause is two claims disagreeing on `kind`, which is a "
            "namespace collision rather than a merge — check what each adapter "
            "believes this identifier is."
        ),
    ))

    if entity is not None:
        # 4. By name.
        hits = [h for h in search(index, identifier) if h.entity.id == identifier]
        steps.append(Step(
            name="search-reachable",
            ok=bool(hits),
            detail="resolves by identifier" if hits else "search does not resolve it",
            remedy=None if hits else (
                "the identifier is in the index but the search resolver does "
                "not return it — a defect in the console, not in your "
                "configuration. File it."
            ),
        ))

        # 5. By structure.
        in_kind = any(e.id == identifier for e in index.of_kind(entity.kind))
        steps.append(Step(
            name="structure-reachable",
            ok=in_kind,
            detail=(
                f"listed under /{entity.kind.route}" if in_kind
                else f"absent from the generated /{entity.kind.route} listing"
            ),
            remedy=None if in_kind else (
                "in the index but not in its own kind's listing — a console "
                "defect, since navigation is generated from the same index."
            ),
        ))

        # 6. By relation — the one that is usually missing.
        inbound = index.inbound(identifier)
        steps.append(Step(
            name="relation-reachable",
            ok=bool(inbound),
            detail=(
                f"{len(inbound)} inbound edge(s)" if inbound
                else "nothing links to it"
            ),
            remedy=None if inbound else (
                "reachable by name and by structure but not by relation, so it "
                "is findable only by someone who already knows it exists "
                "(§3.1). Declare `produces`/`consumes` in its emission, or the "
                "artifact it reads, and the reverse edge is derived."
            ),
        ))

    broken = next((s for s in steps if not s.ok), None)
    return Diagnosis(
        identifier=identifier,
        steps=tuple(steps),
        broken=broken,
        facts={
            "adapters_asked": adapters_asked,
            "claim_classes": sorted({c.claim_class.name for c in claims}),
        },
    )


def render_text(d: Diagnosis) -> str:
    """The CLI rendering. One line per link, the broken one carrying a remedy."""
    lines = [d.summary(), ""]
    for step in d.steps:
        mark = "ok  " if step.ok else "FAIL"
        lines.append(f"  [{mark}] {step.name}: {step.detail}")
        if not step.ok and step.remedy:
            lines.append(f"         → {step.remedy}")
            break  # the FIRST broken link; the rest are consequences of it
    return "\n".join(lines)


def as_dict(d: Diagnosis) -> dict[str, object]:
    return {
        "identifier": d.identifier,
        "ok": d.ok,
        "summary": d.summary(),
        "broken": d.broken.name if d.broken else None,
        "remedy": d.broken.remedy if d.broken else None,
        "steps": [
            {"name": s.name, "ok": s.ok, "detail": s.detail, "remedy": s.remedy}
            for s in d.steps
        ],
        "facts": dict(d.facts),
    }
