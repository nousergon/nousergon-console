"""Build-time namespace uniqueness (§3.6).

**This is not the same check as `merge.NamespaceCollision`, and the difference
is the whole point of the module.**

`merge.py` resolves what happens when several *sources* describe one identifier
at refresh time: normally a merge, and — on a `kind` disagreement — a raise,
because no precedence rule can honestly resolve "two things, one name". That
behaviour is correct and this module does not touch it.

What it cannot do is see a collision *before* it is served. Two registry rows
declaring the same `component_id` with the same kind merge silently, so the
second row shadows the first: its owner, its authority tier and its declared
lifecycle quietly lose, and nothing anywhere says so. §3.6 forbids exactly that
— *"slug uniqueness asserted at build time, with a collision failing the build
rather than shadowing"*.

The split that makes this checkable:

- The **declared** namespace — registry rows, their descriptors, the generated
  page slug for each registry — is knowable from committed files alone. An
  identifier appearing twice there is an authoring mistake, always, and can be
  caught on the pull request that introduces it.
- The **observed** namespace is not knowable in advance, which is why the
  runtime path stays as it is.

So this module reads declaration sources off disk and returns collisions. It
runs in CI (`python -m console check-namespace`) and in the test suite; it is
deliberately NOT wired into `build_index`, because a serving index that refuses
to build over a shadowed row would convert an authoring mistake into an outage
— the failure mode `merge.py`'s own docstring was written against.

`alpha-engine-config-I5880` is the same clause's other half, on the incumbent
dashboard's 74 numeric-prefixed view slugs. Different repo, different fix.
"""
from __future__ import annotations

import dataclasses
import os
from typing import Any

import yaml


@dataclasses.dataclass(frozen=True)
class Collision:
    """One identifier declared more than once, with every place it came from."""

    identifier: str
    scope: str
    sources: tuple[str, ...]

    def render(self) -> str:
        return "{} {!r} declared {} times: {}".format(
            self.scope, self.identifier, len(self.sources), ", ".join(self.sources)
        )


class DeclaredNamespaceCollision(Exception):
    """Raised by `assert_unique`. Carries every collision, not just the first —
    a build error that reports one of four duplicates gets fixed four times."""

    def __init__(self, collisions: list[Collision]):
        self.collisions = collisions
        super().__init__(
            "declared namespace is not unique (console-policy.md §3.6):\n  "
            + "\n  ".join(c.render() for c in collisions)
        )


def _registry_entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Every configured registry, in `build_index`'s own order.

    Mirrors `config.build_index` rather than reimplementing it loosely: a check
    reading a different set of registries than the index does is a check that
    passes for the wrong reason.
    """
    entries = [config["registry"]] if config.get("registry") else []
    entries.extend(config.get("registries") or [])
    return entries


def _row_aliases(body: dict, alias_field: str) -> list[str]:
    """Mirrors `yaml_directory._aliases` — a scalar or a list, never raises."""
    raw = body.get(alias_field)
    if not raw:
        return []
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    return [str(a) for a in raw if a]


def _rows(path: str, id_field: str, alias_field: str = "alias_ids") -> list[tuple[str, str]]:
    """(identifier, file) for every readable row in one registry directory,
    AND for every alias it declares (§3.6, alpha-engine-config-I8779).

    An alias is a second name the same declared row is served under — a
    component id colliding with someone ELSE's alias, or two rows aliasing the
    same id, shadows exactly the way two component ids would, so it is checked
    in the same pass rather than only the primary `id_field`.

    An unreadable or malformed file is SKIPPED rather than raising. This check
    answers one question — is any identifier declared twice — and a parse error
    is a different finding with a different owner; failing here on it would
    make a YAML typo look like a namespace collision.
    """
    found: list[tuple[str, str]] = []
    if not path or not os.path.isdir(path):
        return found
    for entry in sorted(os.listdir(path)):
        if not entry.endswith((".yaml", ".yml")):
            continue
        full = os.path.join(path, entry)
        try:
            with open(full) as fh:
                body = yaml.safe_load(fh) or {}
        except Exception:  # noqa: BLE001 — see docstring; not this check's finding
            continue
        if not isinstance(body, dict):
            continue
        identifier = body.get(id_field)
        if identifier:
            found.append((str(identifier), full))
        for alias_id in _row_aliases(body, alias_field):
            found.append((alias_id, full))
    return found


def check(config: dict[str, Any]) -> list[Collision]:
    """Every declared-namespace collision this configuration would serve.

    Two scopes, checked separately because they fail differently:

    - `component` — one `component_id` in two registry files. Across ALL
      configured registries, not per-directory: two registries each declaring
      `crucible-executor` is the case that shadows hardest, since the losing
      row's whole declaration is invisible.
    - `registry` — two registries configured under one name. Their generated
      index pages (§7) would share a slug, so one page renders and the other
      silently does not exist.
    """
    collisions: list[Collision] = []

    seen: dict[str, list[str]] = {}
    for ordinal, reg in enumerate(_registry_entries(config), start=1):
        for identifier, source in _rows(
            reg.get("path"),
            reg.get("id_field", "component_id"),
            reg.get("alias_field", "alias_ids"),
        ):
            seen.setdefault(identifier, []).append(source)
    for identifier, sources in sorted(seen.items()):
        if len(sources) > 1:
            collisions.append(
                Collision(identifier=identifier, scope="component",
                          sources=tuple(sources))
            )

    names: dict[str, int] = {}
    for ordinal, reg in enumerate(_registry_entries(config), start=1):
        names[reg.get("name", f"registry-{ordinal}")] = names.get(
            reg.get("name", f"registry-{ordinal}"), 0
        ) + 1
    for name, count in sorted(names.items()):
        if count > 1:
            collisions.append(
                Collision(identifier=name, scope="registry",
                          sources=tuple([name] * count))
            )

    return collisions


def assert_unique(config: dict[str, Any]) -> None:
    """Raise `DeclaredNamespaceCollision` if this configuration declares any
    identifier twice. The build-failing half of §3.6."""
    collisions = check(config)
    if collisions:
        raise DeclaredNamespaceCollision(collisions)
