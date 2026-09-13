"""The §4.3 landing view's computed facts, assembled ONCE per `Index`
(alpha-engine-config-I10615).

`render/html.py::landing_page` and `render/json.py::_landing` used to
recompute the same nine numbers, the exception list, the decision queue and
the milestone pane independently, per request — `landing_exceptions`,
`index.conflicts()`, `index.reachability()`, `index.registry_coverage()`,
`index.decision_queue()`, `index.population_completeness()`,
`index.transparency_gap()`, `render.json.numbers`, `milestone_predicates.
evaluate` and `journal_report`. All of them are pure functions of one
already-built, immutable `Index` (§5.6), so nothing about a second request
against the SAME index could ever produce a different answer — the repeat
computation bought nothing but the 2.3-6.8s of per-request latency measured
on the box, which is long enough to trip `box_health.sh`'s 3s HTTP probe
whenever a rebuild is running (roughly half the time, since a build costs
152s against a ~330s cadence).

`build()` is called exactly once per `Index` — by `config.build_index`, on
the supervisor thread, at the end of a real build (warm: no request ever
pays for it), or lazily by `Index.landing_model()` for an `Index` built
directly (every test, `console index --dump`). Either way, both renderers
read the same `LandingModel` rather than each computing their own.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from ..model.entity import Entity


@dataclasses.dataclass(frozen=True)
class LandingModel:
    """Every fact the landing view (HTML or JSON) renders, computed once.

    Holds ENTITIES and raw dicts, not markup or wire shapes — each renderer
    still does its own presentation (HTML strings, JSON projection); only the
    underlying computation is shared, so a change to how a number is
    formatted for one representation cannot silently change the other's
    input.
    """

    exceptions: list[Entity]
    conflicts: list[Entity]
    reachability: dict[str, Any]
    registries: dict[str, Any]
    queue: list[Entity]
    completeness: dict[str, Any]
    gap: dict[str, Any]
    numbers: dict[str, Any]
    milestones: list[dict[str, Any]]
    milestone_journal: list[dict[str, Any]]


def build(index: Any) -> LandingModel:
    """Compute every landing fact once, from an already-finalized `Index`.

    Order matters only where a later computation reuses an earlier result
    (`numbers` takes the already-computed `reachability`/`completeness`
    rather than recomputing them a second time inside this one pass) — see
    `render.json.numbers`'s optional `reachability`/`completeness` params,
    added alongside this for the same reason.
    """
    from ..render.html import landing_exceptions
    from ..render.json import numbers as _numbers
    from .milestones import evaluate as evaluate_milestones
    from .milestones import journal_report as milestone_journal_report

    exceptions = landing_exceptions(index)
    conflicts = index.conflicts()
    reach = index.reachability()
    registries = index.registry_coverage()
    queue = index.decision_queue()
    completeness = index.population_completeness()
    gap = index.transparency_gap()
    n = _numbers(
        index, exceptions, conflicts, gap,
        reachability=reach, population_completeness=completeness,
    )
    milestones = evaluate_milestones(index, n)
    recorded = [dict(r) for r in milestone_journal_report(index)]
    return LandingModel(
        exceptions=exceptions,
        conflicts=conflicts,
        reachability=reach,
        registries=registries,
        queue=queue,
        completeness=completeness,
        gap=gap,
        numbers=n,
        milestones=milestones,
        milestone_journal=recorded,
    )
