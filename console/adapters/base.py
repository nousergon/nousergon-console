"""The adapter interface — the product's public contract (`console-policy.md` §2.3).

An adapter is a function from configuration to entities and edges. It knows
exactly one source and nothing else knows that source at all. This module is
the only coupling point between the index and any source's shape.

The rules, made structural:

- **One source per adapter.** ``fetch`` receives only its own ``config`` dict.
  There is no parameter through which a second source, or another adapter's
  output, could be passed — cross-source relations are formed by the index
  over entity identifiers, never here (§2.3).
- **Configured, never hardcoded.** Every literal naming a bucket, key
  pattern, ARN, host, port, path or component comes from ``config``. A
  topology literal in adapter source is a build finding (§2.3, §1.1).
- **Failure is a state, not an exception.** ``fetch`` returns
  ``AdapterStatus.FAILED`` with entities UNREPORTED when the source is
  unreachable; it never raises the surface empty (§2.3).
- **Honest about absence.** What the source cannot supply is named in
  ``AdapterResult.unavailable``, not guessed (§2.3).
"""
from __future__ import annotations

from typing import Any, Protocol

from ..model.envelope import AdapterResult


class Adapter(Protocol):
    """The contract every adapter satisfies.

    Implementations are callables shaped like ``fetch(config)``. A concrete
    adapter declares its ``name`` and the entity ``produces`` kinds it can
    emit so the index and the §9 self-grading numbers know its surface before
    it runs.
    """

    #: The adapter's configured name (matches `config.example.yaml` `name:`).
    name: str

    #: The entity kinds this adapter can emit. Informational — the index does
    #: not require an adapter to emit every kind it lists on every pass.
    produces: tuple[str, ...]

    def fetch(self, config: dict[str, Any]) -> AdapterResult:
        """Read this adapter's one source and return the projection.

        ``config`` is this adapter's own configuration subtree only. The
        implementation must not reach outside it — to another source, to
        another adapter, or to shared mutable state.
        """
        ...
