"""§9.5 orphan counts and §9.6 staleness honesty.

The two remaining §9 numbers that are pure graph/registry properties, as
distinct from execution (§9.3, in `reachability.py`), history (§9.8's git
archaeology, in `onboarding.py`) or a standing question set (§9.4, in
`console/qa/questions.py`). Kept together because both are cheap, pure
functions of an already-built `Index` and are recomputed on every query
rather than cached (matching `reachability()` and `conflicts()`, not the
build-time-cached `onboarding_cost`/`answer_latency`).
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..model.kinds import State
from ..render.panes import orphan_counts as _pane_orphan_counts

#: Rendered values that ALREADY say "this is stale" — a row carrying one of
#: these has disclosed its own age (§5.2 doing its job), so it is not a
#: staleness-HONESTY finding even though it is, in fact, stale.
#:
#: The four OBSERVED states say "it should have reported and did not". The
#: three DECLARED states (`observability-policy.md` §8.3: declared in the
#: registry, never inferred) say "it was not expected to report at all", which
#: is a STRONGER disclosure of the same age, not a weaker one — a row stating
#: `lifecycle: disabled` is the most explicit account of its own staleness the
#: twelve-state vocabulary can carry. `render/html.py::EXCEPTION_STATES`
#: already excludes exactly these three for exactly this reason ("a decision
#: already taken"), and §9.6 disagreeing with it made a deliberately-off
#: component read as a surface that is lying about freshness. That inversion
#: is not just cosmetic: it is UNCLEARABLE by honesty — the only way to drop
#: the count would be to switch the component back on (alpha-engine-config
#: I6970/I6971, where registering two disabled components as `disabled` cleared
#: §9.1's `unregistered` while leaving §9.6 pinned at the same two rows).
_DECLARED_LIFECYCLE_STATES = frozenset(
    {State.DISABLED, State.DEPRECATED, State.RETIRED}
)
_DISCLOSED_COMPONENT_STATES = frozenset(
    {State.MISSED, State.STALLED, State.UNREPORTED, State.ABSENT}
) | _DECLARED_LIFECYCLE_STATES
_DISCLOSED_VALUES = frozenset(
    {"stale", "no-freshness-stamp", "no-cadence-declared", "unreadable"}
)


def orphan_counts(index=None) -> dict[str, object]:
    """§9.5 — delegates to the pane registry.

    ``index`` is accepted and unused so every §9 number on `Index` shares one
    calling convention (`index.<number>()`); the orphan check is a property of
    the pane registry and the closed `Kind` set, not of any particular graph.
    """
    return _pane_orphan_counts()


def staleness_threshold_seconds(
    cadence_seconds: float, source_cadence_seconds: float, staleness_factor: float
) -> float:
    """How old an `as_of` may be before the row is genuinely stale.

    Two terms, because the age of an `as_of` at query time is the sum of two
    independent delays:

    - **the subject's own** — how long since it last did the thing it is
      declared to do. Bounded by its declared cadence, which is a MAXIMUM GAP
      BETWEEN FIRES rather than a nominal period, and multiplied by
      `staleness_factor` because that is the tolerance being expressed: a
      judgement about whether the component is behaving.
    - **the observer's own** — how long since the source that supplied the
      `as_of` last looked. Bounded by that source's declared poll cadence and
      added FLAT, with no factor: it is not a tolerance for anything, it is a
      declared machine property of the observation path, and inflating it
      would buy detection latency nobody asked for.

    Without the second term a component can only be observed fresh when its
    declared cadence exceeds its observer's — and where the two are equal the
    verdict is decided entirely by the phase offset between two schedules.
    Measured on the live surface 2026-08-13: `staleness_honesty` returned
    1, 0, 0, 1, 0, 0, 1, 2, 2, 0 violations over 31 minutes with nothing
    changing, the members being whichever of two healthy 15-minute EventBridge
    probes happened to be caught late by a 900s adapter
    (alpha-engine-config-I7126). The docstring below already named that shape
    as the one this number must never take: UNCLEARABLE by honesty.

    It cannot make a genuinely stale row read fresh. The widening is bounded
    by the observing source's OWN declared cadence, so a component that stops
    reporting still crosses the threshold — one observer period later than
    before, which is the floor any polling path can deliver. It widens; it
    never removes.
    """
    return cadence_seconds * staleness_factor + source_cadence_seconds


def staleness_honesty(
    index, now: datetime | None = None, staleness_factor: float = 1.5
) -> dict[str, object]:
    """§9.6 — rows older than their OWN declared cadence with no staleness
    marker. Target 0.

    Independently RE-DERIVES staleness from each entity's own declared cadence
    (currently `detail["cadence_minutes"]`, the one per-entity cadence the
    fleet's envelope carries — `checks_envelope.py`) and its `as_of`, rather
    than trusting the state an adapter already assigned. An honesty check that
    only re-reads the thing it is checking would never find the bug it exists
    for: an adapter whose staleness branch stopped firing while its `status`
    field kept reporting `ok`.

    Only entities carrying ALL THREE of a declared cadence, an as-of, and a
    poll cadence on the source that supplied that as-of are auditable; the
    rest are excluded from the denominator rather than counted as a pass
    (§5.3) — a row with nothing to check it against is unaudited, not honest.
    The third is the one added by alpha-engine-config-I7126: a source that
    does not say how often it looks cannot say how old its own reading is
    allowed to be, and defaulting the term to zero is the assumption that
    produced the flap. Excluded rows are NAMED in `unauditable`, so the
    exclusion is visible rather than a quiet shrink of the denominator.

    Refuses to render at all — `computable: False` with a reason, `count` and
    `of` both `None` — when the population itself is not established: a
    DECLARED registry that could not be read this pass, or no auditable row at
    all. `{count: 0, of: 0}` was observed live for one tick and is
    indistinguishable from "all clear" over a real population, which is §5.3's
    forbidden shape (`CN-5.3-no-aggregate-over-incomplete-input`).

    Note the deliberate difference from §9.1: this number does NOT require a
    successful declaration pass. Its population is every row carrying a
    cadence, and a checks-envelope body declares one without any registry
    being configured at all — refusing there would blank a computable audit.
    What it cannot tolerate is a registry that WAS declared and then failed:
    that silently removes rows from the denominator.
    """
    now = now or datetime.now(timezone.utc)
    broken = index.broken_registries() if hasattr(index, "broken_registries") else []
    if broken:
        return _refused(
            "declared registries unreadable this pass: " + ", ".join(broken)
            + " — their rows are silently missing from the denominator, so a "
            "count over what happened to load would report a fraction of the "
            "audited population as if it were all of it (§5.3)"
        )

    checked: list[str] = []
    violations: list[str] = []
    unauditable: dict[str, str] = {}
    for ent in index.all():
        cadence_minutes = ent.detail.get("cadence_minutes")
        as_of_provenance = ent.source_of("as_of")
        as_of = as_of_provenance.as_of or ent.provenance.as_of
        if not cadence_minutes or not as_of:
            continue  # nothing declared to check against — not a population member
        try:
            cadence_seconds = float(cadence_minutes) * 60.0
            ts = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        source_cadence = as_of_provenance.cadence_seconds
        if source_cadence is None:
            # Declared absence, never defaulted (§2.3). This row HAS a cadence
            # and an as-of, so it looks auditable — and auditing it would mean
            # assuming its observer polls instantaneously.
            unauditable[ent.id] = (
                f"source {as_of_provenance.source!r} supplied `as_of` but "
                "declares no poll cadence, so the observation lag inside that "
                "timestamp is unbounded"
            )
            continue
        checked.append(ent.id)
        age = (now - ts).total_seconds()
        if age <= staleness_threshold_seconds(
            cadence_seconds, float(source_cadence), staleness_factor
        ):
            continue  # genuinely fresh
        disclosed = (
            isinstance(ent.state, State) and ent.state in _DISCLOSED_COMPONENT_STATES
        ) or (
            isinstance(ent.state, str) and ent.state.strip().lower() in _DISCLOSED_VALUES
        )
        if not disclosed:
            violations.append(ent.id)
    if not checked:
        return _refused(
            "no row carries a declared cadence, an as-of AND a source that "
            "declares its own poll cadence — an empty population renders "
            "`0 of 0`, which reads exactly like all-clear (§5.3)",
            unauditable=unauditable,
        )
    return {
        "count": len(violations),
        "of": len(checked),
        "violations": sorted(violations),
        "computable": True,
        "unauditable": dict(sorted(unauditable.items())),
    }


def _refused(reason: str, unauditable: dict[str, str] | None = None) -> dict[str, object]:
    """§5.3's refusal shape, matching `onboarding_cost`'s `computable: False`.

    `count`/`of` are `None` rather than 0 — a renderer that reads them as
    numbers gets nothing to render, which is the point: no data is never
    rendered as green (`principles.md` §2.7).
    """
    return {
        "count": None,
        "of": None,
        "violations": [],
        "computable": False,
        "reason": reason,
        "unauditable": dict(sorted((unauditable or {}).items())),
    }
