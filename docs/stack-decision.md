# Stack decision — nousergon-console

**Decided 2026-07-31, Brian ruling (this session).** README's roadmap left the implementation stack open ("decided with item 1"). This records the choice and the reasoning, per `principles.md` §2.4 (name the SOTA and the delta).

## Decision: Python + stdlib server

The adapter contract and the seven-kind entity model are stack-independent; this decision covers only the **serving and rendering** layer.

| Concern | Choice |
|---|---|
| Adapters, entity model, index, relation graph, search | Python 3, stdlib only where possible |
| HTTP serving | `http.server`-based router, `/<kind>/<id>` routes |
| Rendering | Server-side HTML; minimal vanilla-JS enhancement for URL sync (§3.2) |
| State | **None persisted.** Index is in-memory at start, derived from adapters (§5.6) |
| Testing | `pytest` + recorded fixtures, no live credentials |
| Build step | None. No transpile, no bundler, no database |

## Why this is the SOTA here, and the delta

**SOTA for a persists-nothing, read-only, one-operator index** is the thinnest serving layer that keeps the graph testable and the artifact self-hostable with zero external services. That is a stdlib HTTP server plus server-rendered HTML — not a SPA framework, not a database, not a message bus. Every category of state the catalog products (Backstage, Port, OpenMetadata) carry — Postgres, ingestion pipelines, ES/Kafka — is exactly what §5.6 forbids this tool owning.

- **Matches the org's Python baseline** and CI shape; no new toolchain enters the fleet for one process.
- **Keeps the relation graph and §9.3 reachability testable in pytest** against fixtures — the property `groom-sweep-policy.md` §8.1 requires of any control loop, applied here to the index.
- **No build step** means a clean checkout runs the console with `python -m console` and nothing else.

**Delta:** rendering is server-templated, so rich client-side interactivity is thinner than a SPA. Accepted — the surface is read-only, exception-first, and serves one operator; the interaction budget (§4.2) is ≤4 clicks to any answer, which server rendering plus URL-driven state meets without a client framework.

## The rejected alternative, and why

**Node + vanilla JS (the symposion stack)** was the in-fleet familiar option. It was declined on evidence, not preference: symposion's own `public/app.js` (2,584 lines) shipped with **no `pushState`, no hash routing, no `URLSearchParams`** — the precise defect `nous-ergon-ops-I327` exists to close. Re-importing that stack re-imports the failure mode unless the client is held to a hard URL-routing contract from line one; server-side rendering makes §3.2's identity-is-the-URL structural rather than a discipline the client must remember to keep. The rendering layer is where I327's defect lives, so the rendering layer is where the contract is enforced by construction.
