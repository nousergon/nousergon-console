"""nousergon-console — a read-only fleet index that persists nothing.

The package layout mirrors the adapter contract (`console-policy.md` §2.3):

- ``model``    — the seven closed entity kinds and the versioned wire envelope.
- ``adapters`` — one adapter per source of truth; config → entities + edges.
- ``index``    — the typed entity graph, relations, and §9.3 reachability.
- ``search``   — the global identifier resolver (§3.7).
- ``server``   — the stdlib HTTP router; ``/<kind>/<id>`` routes (§3.2).
- ``render``   — server-side HTML honouring the four-field row contract (§5.1).

The console renders; it never owns (§5.6). Nothing in this package writes
durable state about the system — every figure is a projection of a fact that
lives in a source of truth an adapter reads.
"""
