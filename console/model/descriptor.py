"""The component descriptor — one file, and it says where its facts live (§2.6).

**Onboarding a process or module is writing this file.** It is committed beside
the thing it describes, and it binds the component to the places its data
already is: its runs, its artifacts, its metrics, its inputs and outputs.
Nothing about the console changes, and in the normal case nothing about the
module changes either — modules log the metrics they need to expose, and the
descriptor points at that log.

Brian's ruling, 2026-08-03. The defect it names: the binding used to live in the
*adapter's* configuration — bucket, prefix, key pattern — which is **per-source,
not per-component**. So a component whose data landed anywhere no adapter
already pointed cost an edit to the console's own config, and §2.6's zero-edit
test was being met only by components lucky enough to land in the right prefix.
Onboarding was free by coincidence rather than by design.

The descriptor carries **bindings**; a **driver** (§2.7) knows how to read one
source *shape*. Which bucket, group, ARN or table belongs to a component is
here, never in driver source and never in console configuration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

#: Descriptor keys that declare bindings, and the entity kind each produces.
#: A closed set: adding one is a change to this model and to §2.6, not a string
#: somebody puts in a YAML file and hopes about.
BINDING_KEYS: tuple[str, ...] = ("runs", "artifacts", "metrics")

#: Keys that declare lineage. Not bindings — nothing is read to satisfy them;
#: they are statements the component makes about its own edges (§6).
LINEAGE_KEYS: tuple[str, ...] = ("produces", "consumes")


class DescriptorError(Exception):
    """A descriptor the console cannot act on.

    Raised at build time, never at render: a descriptor naming a driver that
    does not exist must fail LOUDLY (§2.7). Rendering the component as merely
    absent would make a typo indistinguishable from a component that is
    genuinely gone, which is `observability-policy.md` §8.3's `RETIRED`/`ABSENT`
    collapse arriving through the configuration layer.
    """


@dataclass(frozen=True)
class Binding:
    """One declared source of facts about one component.

    ``driver`` names a source *shape* (§2.7). ``spec`` is everything that
    driver needs to reach this component's instance of it — a key, an ARN, a
    log location, a query. The console never interprets ``spec``; only the
    named driver does, which is what keeps drivers ignorant of components.
    """

    component_id: str
    kind: str          # one of BINDING_KEYS
    driver: str
    spec: Mapping[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """How `doctor` (§3.9) names this binding when it is the broken one."""
        return f"{self.kind}:{self.driver}"


@dataclass(frozen=True)
class Descriptor:
    """A parsed component descriptor."""

    component_id: str
    row: Mapping[str, Any]
    bindings: tuple[Binding, ...] = ()
    produces: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    source_file: str | None = None


def parse(row: Mapping[str, Any], component_id: str,
          source_file: str | None = None,
          known_drivers: frozenset[str] | None = None) -> Descriptor:
    """Parse one descriptor row into its bindings.

    ``known_drivers`` is checked here rather than at read time so the failure
    names the descriptor and the driver together — "component X binds `metrics`
    to driver `log-souce`" is a fixable message; "unknown driver" is not.
    """
    bindings: list[Binding] = []
    for key in BINDING_KEYS:
        raw = row.get(key)
        if raw is None:
            continue
        # A binding key takes either one mapping or a list of them. Both are
        # natural to write and neither is ambiguous, so both are accepted —
        # a descriptor format that fails on a defensible spelling is a
        # descriptor format people work around.
        specs = raw if isinstance(raw, list) else [raw]
        for spec in specs:
            if not isinstance(spec, Mapping):
                raise DescriptorError(
                    f"{component_id}: `{key}` entry is not a mapping "
                    f"({type(spec).__name__}). Each binding is "
                    "`{driver: <shape>, …}`."
                )
            driver = spec.get("driver")
            if not driver:
                raise DescriptorError(
                    f"{component_id}: `{key}` binding declares no `driver`. "
                    "A binding with no driver names a source shape nobody can "
                    "read, so nothing would be read and nothing would say so."
                )
            if driver == "sql-source":
                _validate_sql_source(spec, component_id)
            if known_drivers is not None and driver not in known_drivers:
                raise DescriptorError(
                    f"{component_id}: `{key}` binds to driver {driver!r}, "
                    f"which does not exist. Known drivers: "
                    f"{', '.join(sorted(known_drivers)) or 'none registered'}. "
                    "This fails the build rather than rendering the component "
                    "absent, because a typo and a genuinely-gone component "
                    "must not look the same (§2.7)."
                )
            bindings.append(Binding(
                component_id=component_id, kind=key, driver=str(driver),
                spec={k: v for k, v in spec.items() if k != "driver"},
            ))

    return Descriptor(
        component_id=component_id,
        row=row,
        bindings=tuple(bindings),
        produces=_string_list(row.get("produces")),
        consumes=_string_list(row.get("consumes")),
        source_file=source_file,
    )


def _string_list(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value if v)


def _validate_sql_source(spec: Mapping[str, Any], component_id: str) -> None:
    """Reject executable or credential-bearing SQL descriptors at build time."""
    forbidden = {"connection_string", "dsn", "password", "url", "uri"}
    if forbidden.intersection(spec):
        raise DescriptorError(
            f"{component_id}: sql-source has an inline credential; descriptors "
            "name a private `credential` reference, never a connection value."
        )
    query = spec.get("query")
    normalized = str(query or "").strip()
    if (not normalized.lower().startswith("select ") or ";" in normalized.rstrip(";")):
        raise DescriptorError(
            f"{component_id}: sql-source query must be one parameterless SELECT statement."
        )
