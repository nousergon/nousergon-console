"""Config loading — the console's only source of topology (§1.1, §2.3).

Everything specific to a deployment lives in `config.yaml` (gitignored) and
nowhere else. The console reads it at start and derives the index, navigation,
search and relation graph from whatever the enabled adapters return — so
adding a component or a source is a config/registry edit, never a code edit
(§3.5). This module loads the file and instantiates the enabled adapters; it
holds no topology of its own.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable

import yaml

from .adapters import (
    changelog_events,
    changelog_retro_feed,
    checks_envelope,
    cloudwatch_metrics,
    declared_registry,
    git_host,
    local_units,
    object_store,
    pipeline_reliability,
    s3_records,
    sql_query,
    sql_source,
    state_machine,
    yaml_directory,
)
from .drivers import KNOWN_DRIVERS, resolve_bindings
from .drivers.context import defaults as driver_defaults
from .index import milestones as milestone_predicates
from .index.build import Supervisor, now_iso
from .index.graph import Index
from .index.onboarding import compute_onboarding_cost
from .model.envelope import AdapterResult, AdapterStatus
from .model.kinds import EXCEPTION_VALUES
from .qa.questions import measure as measure_answer_latency

#: Adapter registry — name → module with a `fetch` callable. Adding a source
#: is adding an adapter here (§2.3); no other wiring changes.
ADAPTERS = {
    "yaml-directory": yaml_directory,
    "git-host": git_host,
    "object-store": object_store,
    "local-units": local_units,
    "state-machine": state_machine,
    "pipeline-reliability": pipeline_reliability,
    "checks-envelope": checks_envelope,
    "cloudwatch-metrics": cloudwatch_metrics,
    "changelog-events": changelog_events,
    "changelog-retro-feed": changelog_retro_feed,
    "declared-registry": declared_registry,
    "sql-source": sql_source,
    "s3-records": s3_records,
    "sql-query": sql_query,
}


def load_config(path: str) -> dict[str, Any]:
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


class ConfigError(Exception):
    """A configuration fragment that cannot be honestly indexed.

    Scoped to the one adapter fragment that raised it (alpha-engine-config-
    I8778) — this is `index/merge.py::NamespaceCollision`'s SIBLING class of
    problem (config asserting something that is not a reading of the fleet),
    not a whole-build failure. `build_index` catches a `ConfigError` raised
    while resolving one adapter fragment and fails only that fragment's
    source (§2.3's "a source failure never empties the surface", applied to
    a bad DECLARATION exactly as it already applies to an unreachable one).
    The two CLI pre-flight commands (`console index`, `console
    check-namespace`) call `validate_config` directly, BEFORE any adapter
    runs, so an author-time mistake still fails the PR loudly (§3.6's
    build-time gate) — only the SERVING path (`console serve` /
    `supervised_index`) degrades per source.
    """


def validate_config(config: dict[str, Any]) -> None:
    """Refuse a config whose declarations would manufacture findings.

    **One rule so far** (alpha-engine-config-I8765): a `declared-registry` may
    not set `default_state` to a member of `EXCEPTION_VALUES`. That default is
    by construction the state of every row NOTHING OBSERVED, so naming an
    exception there asserts a finding about each of them off no reading at all
    — `observability-policy.md` §8.3's "a state derived from the absence of
    evidence", and the exact defect `index/graph.py::_reconcile` already
    refuses for Components ("ABSENT requires a successful discovery pass").
    Live cost of the config edit nobody could flag: 177 of 508 exception rows
    on 2026-08-27.

    Raised, not logged — but only reachable from two places now
    (alpha-engine-config-I8778): the `console index` / `console
    check-namespace` CLI pre-flight (`__main__.py`), which is the strict
    build-time gate CI runs, and `_check_declared_registry_default_state`
    below, which `build_index` calls PER FRAGMENT so one bad fragment fails
    only its own source rather than raising here and emptying the whole
    index (`nousergon-console-PR112`/`PR113` shipped the blanket call that
    did that — measured live 2026-08-27 22:43-22:5xZ: `sources: []`,
    `exceptions: 0`, a blank all-clear-looking surface for ~10 minutes).
    The message names the FRAGMENT so the fix is a one-line edit in a file
    the operator already has open.
    """
    for label, cfg in _declared_registry_configs(config):
        _check_declared_registry_default_state(label, cfg)


def _check_declared_registry_default_state(label: str, cfg: dict[str, Any]) -> None:
    """The one rule in `validate_config`, scoped to one fragment.

    Shared by the strict CLI pre-flight (`validate_config`, called once per
    fragment in a loop) and `build_index`'s per-adapter degrade (called
    inline for the one fragment about to be built) — one rule, one place it
    is checked, so the two paths cannot drift apart on what counts as bad.
    """
    raw = cfg.get("default_state")
    if raw is None:
        return
    if str(raw).strip().lower() in EXCEPTION_VALUES:
        raise ConfigError(
            f"{label}: `default_state: {raw}` is an exception state. A "
            "declared-registry's default is what a row carries when "
            "NOTHING observed it, so this asserts a finding about every "
            "unobserved row off no reading at all (observability-policy.md "
            "§8.3). Use `default_state: unobserved` and wire an observation "
            "half (an `object-store` adapter over the same identifiers) for "
            "the rows a real read can reach."
        )


def _declared_registry_configs(config: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """`(fragment label, adapter config)` for every configured declared-registry.

    Both shapes: a `registry:`/`registries:` entry carries its adapter config at
    the top level, an `adapters:` entry nests it under `config:`. The label is
    the entry's `name`, which is the `config.d/<name>.yaml` fragment filename in
    the fleet's own assembler (`nous-ergon-ops/scripts/console_config.py`), so
    the error names the file to edit.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    registries = ([config["registry"]] if config.get("registry") else [])
    registries.extend(config.get("registries") or [])
    for reg in registries:
        if reg.get("adapter") == "declared-registry":
            out.append((str(reg.get("name", "registry")), reg))
    for entry in config.get("adapters") or []:
        if entry.get("kind") == "declared-registry":
            out.append((str(entry.get("name", "declared-registry")),
                        entry.get("config") or {}))
    return out


def build_index(config: dict[str, Any]) -> Index:
    """Run every enabled adapter once and fold its projection into the index.

    A FAILED adapter contributes its entities as UNREPORTED (handled in the
    index) and does not stop the other adapters — the surface degrades per
    source, never empties (§2.3). A per-fragment `ConfigError` (currently:
    a `declared-registry`'s `default_state` naming an exception state) is
    caught HERE, per fragment, and fails only that fragment's source — never
    a blanket `validate_config(config)` call, which would fail the whole
    build over one bad fragment while every other source stayed healthy
    (measured live 2026-08-27: `sources: []` for ~10 minutes,
    alpha-engine-config-I8778). The strict, whole-config `validate_config` is
    still called directly by the CLI's build-time pre-flight commands
    (`__main__.py`), run before any adapter, so CI still fails a PR that
    introduces the mistake.
    """
    index = Index()
    # Parsed FIRST, before any adapter runs: an unknown binding kind or
    # comparator must fail the build naming the milestone and clause, not
    # render as an absence on a page that otherwise looks fine (§4.5). This is
    # the same build `console index --config config.example.yaml` runs on every
    # PR, so a bad declaration is caught there rather than live.
    declared_milestones = milestone_predicates.parse(config.get("milestones"))
    # One tolerance, set before any claim arrives: the merge's DISABLED/MISSED
    # comparison and §9.6's staleness audit both read it, and they must not be
    # able to disagree about one row (`index/cadence_state.py`).
    index.set_staleness_factor(
        float((config.get("console") or {}).get("staleness_factor", 1.5))
    )
    descriptors: list = []
    # The registry adapter is configured under its own `registry:` block.
    registry_entries = ([config["registry"]] if config.get("registry") else [])
    registry_entries.extend(config.get("registries") or [])
    for ordinal, reg in enumerate(registry_entries, start=1):
        name = reg.get("name", f"registry-{ordinal}")
        index.declare_registry(name)
        if reg.get("adapter") not in ADAPTERS:
            index.add_result(_unknown_adapter(name, reg.get("adapter")))
            continue
        if reg.get("adapter") == "declared-registry":
            try:
                _check_declared_registry_default_state(name, reg)
            except ConfigError as exc:
                index.add_result(_config_error_result(name, exc))
                continue
        module = ADAPTERS[reg["adapter"]]
        result, elapsed = _timed_fetch(module.fetch, {
            **reg, "_name": name,
            "known_drivers": KNOWN_DRIVERS,
        })
        index.add_result(result, elapsed_seconds=elapsed)
        index.render_registry(name)
        # §9.1's per-registry denominator: how many rows this registry
        # offered THIS pass, and whether its adapter could even read it.
        index.record_registry_rows(
            name, count=len(result.entities), ok=result.status is AdapterStatus.OK,
        )
        descriptors.extend(result.descriptors)

    for entry in config.get("adapters", []) or []:
        if not entry.get("enabled"):
            continue
        kind = entry.get("kind")
        module = ADAPTERS.get(kind)
        if module is None:
            index.add_result(_unknown_adapter(entry.get("name", kind), kind))
            continue
        name = entry.get("name", kind)
        if kind == "declared-registry":
            try:
                _check_declared_registry_default_state(str(name), entry.get("config") or {})
            except ConfigError as exc:
                index.add_result(_config_error_result(str(name), exc))
                continue
        cfg = {**entry.get("config", {}), "_name": name}
        result, elapsed = _timed_fetch(module.fetch, cfg)
        index.add_result(result, elapsed_seconds=elapsed)

    # §2.6: every component's own descriptor said where its facts live. Walking
    # those bindings is what makes onboarding cost ONE FILE — a component whose
    # data lands somewhere no adapter points is read because it said where, not
    # because somebody added a prefix to this file.
    for result in resolve_bindings(descriptors, _driver_context(config)):
        index.add_result(result)

    # §9.8: computed once per build (a git subprocess per registry row is not
    # something a request handler should pay for). Defaults to the process's
    # own working directory — the same convention `--config config.yaml`
    # already uses (config.py carries no filesystem literal of its own, §2.3);
    # `console.repo_root` overrides it for a deployment that runs elsewhere.
    console_cfg = config.get("console") or {}
    # §9.7: which component watches this surface from outside it. A fleet fact,
    # so it arrives as configuration and never as a literal here (§2.3).
    index.set_liveness_watcher(console_cfg.get("liveness_watcher"))
    repo_root = console_cfg.get("repo_root") or os.getcwd()
    registry_paths = [str(r["path"]) for r in registry_entries if r.get("path")]
    registry_id_field = next(
        (r.get("id_field", "component_id") for r in registry_entries if r.get("path")),
        "component_id",
    )
    index.set_onboarding_cost(
        compute_onboarding_cost(repo_root, registry_paths, id_field=registry_id_field)
    )
    # §9.4: run the standing question set against the index just built.
    index.set_answer_latency(measure_answer_latency(index))
    # Declarations only — evaluation is per query, over the built graph, and
    # nothing about it is cached (§5.6).
    milestone_predicates.attach(index, declared_milestones)
    return index


def _timed_fetch(fetch: Callable[..., Any], config: dict[str, Any]) -> tuple[Any, float]:
    """Wall-clock one adapter fetch. I7124 deliverable 1: the 93.5s split.

    Measured on the box as one number; without a per-source elapsed the
    cadence decision (deliverable 3) is a guess, and raising refresh_seconds
    to fit a slow build would hide whichever source actually dominates.
    """
    started = time.monotonic()
    return fetch(config), time.monotonic() - started


def _unknown_adapter(name: str, kind: Any) -> AdapterResult:
    """A configured source naming an adapter this build does not have (§2.3).

    It used to `continue` — the source vanished from the surface AND from
    `build_info.adapters`, so nothing said a configured source was not being
    read. That is the exact failure mode of a config applied ahead of the code
    that implements its adapter: the console renders a smaller fleet, entirely
    silently, and looks healthy doing it.

    Not an exception, because one bad config entry must not empty a surface a
    dozen working sources are rendering. It is a FAILED source with the reason
    named, which is what §2.3 asks of every unreachable source.
    """
    return AdapterResult(
        name=str(name),
        status=AdapterStatus.FAILED,
        fetched_at=now_iso(),
        unavailable=(f"adapter:{kind}",),
    )


def _config_error_result(name: str, exc: ConfigError) -> AdapterResult:
    """A per-fragment `ConfigError`, rendered as a FAILED source (§2.3).

    The sibling of `_unknown_adapter`: a configured source this build cannot
    honestly build, named and red on every page, never a build-wide raise
    (alpha-engine-config-I8778). The error text IS the `unavailable` reason —
    it already names the fragment and the fix (`_check_declared_registry_
    default_state`'s message), so the landing page's failing-source row is
    the same one-line edit the operator would get from `console index`.
    """
    return AdapterResult(
        name=name,
        status=AdapterStatus.FAILED,
        fetched_at=now_iso(),
        unavailable=(str(exc),),
    )


def _driver_context(config: dict[str, Any]) -> dict[str, Any]:
    """Console-level facilities a driver may need — never topology (§2.7).

    A clock and a staleness factor. Which bucket, group or table a component
    uses is in that component's descriptor, and nothing here may carry it.
    """
    console_cfg = config.get("console") or {}
    return {
        # Production readers first (`drivers/context.py`) — a build with no
        # injected reader is the LIVE build, and until 2026-08-17 that build
        # had none, so every S3-bound binding failed on the box (config-I7425).
        **driver_defaults(),
        "staleness_factor": float(console_cfg.get("staleness_factor", 1.5)),
        # Values live only in the gitignored runtime config; descriptors carry
        # the stable key, never a DSN or password (§2.7).
        "sql_credentials": dict(config.get("sql_credentials") or {}),
        **(config.get("_driver_context") or {}),
    }


#: How often the index is rebuilt when the config does not say. Deliberately a
#: minute rather than an hour: §5.9's failure is a surface that looks live and
#: is not, and the cost of being wrong in the cheap direction is a few extra
#: reads of sources the console already reads.
DEFAULT_REFRESH_SECONDS = 60.0


def supervised_index(config: dict[str, Any]) -> Supervisor:
    """An index that rebuilds on a cadence and swaps atomically (§5.9).

    The whole of "the console renders, it never owns" (§5.6) made operational:
    nothing is persisted, so keeping the surface current is re-deriving it, and
    the only state that survives a rebuild is the reference to it.
    """
    refresh = float(
        (config.get("console") or {}).get("refresh_seconds")
        or DEFAULT_REFRESH_SECONDS
    )
    # defer_first_build: the caller binds a port right after this returns, and
    # a full pass takes 93.5s on the live box. See Supervisor.__init__.
    return Supervisor(
        lambda: build_index(config), refresh_seconds=refresh,
        defer_first_build=True,
    )
